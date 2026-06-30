# dicorina

Pure-Python DICOM + DICOMweb pass-through proxy for a C-MOVE-only PACS, built as a thin application layer over the [`dimsechord`](https://pypi.org/project/dimsechord/) core library. To develop, install dependencies and run tests: `uv sync && uv run pytest`.

## Run / Deploy

### Development

```bash
DICORINA_CONFIG=deploy/config.example.toml uv run dicorina
```

### Production (systemd)

The server needs only Python 3.12 with the `venv` module (e.g. `python3.12-venv` on Debian/Ubuntu) — `uv` is **not** required. `install.sh` creates a `.venv` under the install dir and `pip install`s the package and its dependencies (including `dimsechord`) from PyPI.

1. Copy and edit the example config: `cp deploy/config.example.toml /etc/dicorina/config.toml`
2. Run `sudo deploy/install.sh` from the project root — this provisions the `dicorina` system user, creates `/var/cache/dicorina`, and sets ownership on the install dir.
3. Enable and start the service: `systemctl enable --now dicorina`

The unit runs the `dicorina` console script, which reads `DICORINA_CONFIG` and binds uvicorn to
`http.bind_host`/`http.bind_port` from the config. One process, two listeners: uvicorn serves
HTTP on the configured port; the pynetdicom DIMSE AE (C-FIND/C-MOVE/C-ECHO) binds
`dimse.listen_ip`/`dimse.listen_port` inside the lifespan; eviction and the healthcheck run as
in-process asyncio tasks.

**DIMSE port firewall:** the HTTP listener binds `http.bind_host` (default `127.0.0.1`). The
DIMSE face binds `dimse.listen_ip` (default `0.0.0.0`) and must be reachable from the PACS.
Restrict access with a host-level IP-allowlist, for example:

```bash
ufw allow from <PACS_IP> to any port <dimse.listen_port>
```

## Auth

The HTTP API (`/dicom-web/*`) ships open by default (`auth_token = ""`). The HTTP face binds `127.0.0.1` and sits behind Clarinet's nginx reverse proxy (same-origin OHIF). To enforce token authentication, set `DICORINA_AUTH_TOKEN` environment variable or configure `[http] auth_token` in config. When enabled, requests must include either `Authorization: Bearer <token>` or `X-Internal-Token: <token>` header; both use constant-time comparison. Browsers cannot add custom headers, so when authentication is enforced, nginx must inject `X-Internal-Token` on proxied `/dicom-web/` requests (or upgrade to nginx `auth_request` + Clarinet `/api/auth/me`). The DIMSE face (C-MOVE/C-GET/C-FIND) is always protected independently by firewall IP-allowlist, called-AET check, and AET allowlist, regardless of HTTP token setting.

## E2E (multi-VM)

`staging/vm-net/` runs the proxy across 4 QEMU/KVM nodes (Orthanc PACS, dicorina via
install.sh+systemd, two clients). Build goldens once, then run:

    bash staging/vm-net/build-golden.sh   # cached; FORCE_REBUILD=pacs|client|all
    bash staging/vm-net/run.sh            # boots all 4, asserts S0-S7 on the host

Env: `WORK=<disk dir>` (default /tmp/dicorina-vm-net), `INSTANCES_PER_STUDY` (default 50),
`TIMEOUT`. Needs /dev/kvm + uv. Pure-module units run in the normal suite (`uv run pytest`).
