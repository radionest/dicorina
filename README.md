# dicorina

Pure-Python DICOM + DICOMweb pass-through proxy for a C-MOVE-only PACS, built as a thin application layer over the [`dimsechord`](https://pypi.org/project/dimsechord/) core library. To develop, install dependencies and run tests: `uv sync && uv run pytest`.

## Run / Deploy

### Development

```bash
DICORINA_CONFIG=deploy/config.example.toml uv run dicorina
```

### Production (systemd)

The server needs only Python 3.12 with the `venv` module (e.g. `python3.12-venv` on Debian/Ubuntu) — `uv` is **not** required. `install.sh` creates a `.venv` under the install dir and `pip install`s the package and its dependencies (including `dimsechord`) from PyPI. Internet access to PyPI is required during install.

Production deps are **intentionally not pinned**: `pip install .` resolves the version ranges in `pyproject.toml` fresh from PyPI, so two installs at different times may pull different patch/minor versions. `uv.lock` pins dev and E2E (`uv sync`) but is not used in production. If you need a reproducible production build, pin versions in `pyproject.toml` or install from an exported lock (`uv export > requirements.txt` then `pip install -r requirements.txt`).

1. Copy and edit the example config: `cp deploy/config.example.toml /etc/dicorina/config.toml`
2. Run `sudo deploy/install.sh` from the project root — this provisions the `dicorina` system user, creates `/var/cache/dicorina`, and sets ownership on the install dir.
3. Enable and start the service: `systemctl enable --now dicorina`

The unit runs the `dicorina` console script, which reads `DICORINA_CONFIG` and binds uvicorn to
`http.bind_host`/`http.bind_port` from the config. One process, two listeners: uvicorn serves
HTTP on the configured port; the pynetdicom DIMSE AE (C-FIND/C-MOVE/C-STORE/C-ECHO) binds
`dimse.listen_ip`/`dimse.listen_port` inside the lifespan; eviction and the healthcheck run as
in-process asyncio tasks.

**DIMSE port firewall:** the HTTP listener binds `http.bind_host` (default `127.0.0.1`). The
DIMSE face binds `dimse.listen_ip` (default `0.0.0.0`) and must be reachable from the PACS.
Restrict access with a host-level IP-allowlist, for example:

```bash
ufw allow from <PACS_IP> to any port <dimse.listen_port>
```

**C-STORE relay:** clients can C-STORE to the DIMSE face; each instance is
forwarded 1:1 to the PACS and the PACS's status is returned verbatim (no queue —
if the PACS is down, the client's store fails and the client retries). The PACS
must accept C-STORE associations from `pacs.store_aet` (default: `dimse.aet`) —
register that AET on the PACS before enabling clients.

## Logs

dicorina installs its own root logging handler at startup. This matters because
uvicorn's logging config attaches handlers to the `uvicorn*` loggers only and
leaves the root logger without one — everything below WARNING from `dicorina`,
`dimsechord` and `pynetdicom` would otherwise be discarded by
`logging.lastResort` before reaching journald. Levels are set in `[logging]`
(see `deploy/config.example.toml`) or overridden with `DICORINA_LOG_LEVEL` /
`DICORINA_PYNETDICOM_LOG_LEVEL`.

**Reading a load incident.** The DIMSE face logs one line when an association
is accepted, rejected or ends, and one per finished C-FIND/C-MOVE — at WARNING
once it exceeds `slow_operation_seconds`, because a slow operation holds both
its SCP worker thread and its association slot for its whole duration. On top
of that a snapshot line lands every `snapshot_interval_seconds`:

    load: dimse_assoc=7/10 store_sessions=3 ops[find=2(peak 5) store=1(peak 8)]
    threads=48 executor=0/32 loop_lag_ms=4

- `dimse_assoc=live/max` — inbound associations against pynetdicom's
  `maximum_associations` (default 10). At the cap, pynetdicom refuses every
  further request with an A-ASSOCIATE-RJ *above* dicorina's handlers; clients
  see a rejected or timed-out connect. The snapshot escalates to WARNING here,
  and each refusal is logged with its decoded reason.
- `ops[...]` — DIMSE handlers in flight, with the peak since start. Compare
  against the pool's **totals**, which the startup line spells out: the caps
  are per AET, so capacity is `per_aet_cap x len(pool.members)` concurrent
  C-MOVEs and `per_aet_find_cap x len(pool.members)` concurrent C-FINDs.
  Handlers queued behind an exhausted pool keep their association slots for
  the whole wait. Note this counter covers the DIMSE face only, while QIDO and
  WADO lease the same pool — a C-FIND can be queued behind HTTP traffic that
  `ops[...]` does not show.
- `executor=queued/workers` — asyncio's default thread executor, which every
  streaming QIDO/WADO response occupies for its whole lifetime. A non-zero
  queue means further `to_thread` calls are waiting, and HTTP requests hang
  without raising.
- `loop_lag_ms` — how late the event loop resumed a timer. Sustained lag means
  synchronous work is blocking the loop, which stalls HTTP and C-MOVE planning
  together.

## Auth

The HTTP API (`/dicom-web/*`) ships open by default (`auth_token = ""`). The HTTP face binds `127.0.0.1` and sits behind Clarinet's nginx reverse proxy (same-origin OHIF). To enforce token authentication, set `DICORINA_AUTH_TOKEN` environment variable or configure `[http] auth_token` in config. When enabled, requests must include either `Authorization: Bearer <token>` or `X-Internal-Token: <token>` header; both use constant-time comparison. Browsers cannot add custom headers, so when authentication is enforced, nginx must inject `X-Internal-Token` on proxied `/dicom-web/` requests (or upgrade to nginx `auth_request` + Clarinet `/api/auth/me`). The DIMSE face (C-MOVE/C-FIND/C-STORE) is always protected independently by the host firewall IP-allowlist and the called-AET check, regardless of HTTP token setting; the `[dimse.allowlist]` table additionally restricts C-MOVE *destinations* only — inbound callers (including C-STORE writers) are not authorized by AET, so size the DIMSE-port firewall for every storage client, not just the PACS.

## E2E (multi-VM)

`staging/vm-net/` runs the proxy across 4 QEMU/KVM nodes (Orthanc PACS, dicorina via
install.sh+systemd, two clients). Build goldens once, then run:

    bash staging/vm-net/build-golden.sh   # cached; FORCE_REBUILD=pacs|client|all
    bash staging/vm-net/run.sh            # boots all 4, asserts S0-S8 on the host

Env: `WORK=<disk dir>` (default /tmp/dicorina-vm-net), `INSTANCES_PER_STUDY` (default 50),
`TIMEOUT`. Needs /dev/kvm + uv. Pure-module units run in the normal suite (`uv run pytest`).

A latency bench (dicorina overhead vs a direct client→PACS path) reuses the same
stand — see the "Latency bench" section in `staging/vm-net/README.md`.
