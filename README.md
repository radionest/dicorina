# dicorina

Pure-Python DICOM + DICOMweb pass-through proxy for a C-MOVE-only PACS, built as a thin application layer over the prebuilt `dimsechord` core library (sourced from `../dimsechord` via editable path install). To develop, install dependencies and run tests: `uv sync && uv run pytest`.

## Run / Deploy

### Development

```bash
DICORINA_CONFIG=deploy/config.example.toml uv run dicorina
```

### Production (systemd)

1. Copy and edit the example config: `cp deploy/config.example.toml /etc/dicorina/config.toml`
2. Run `sudo deploy/install.sh` from the project root — this provisions the `dicorina` system user, creates `/var/cache/dicorina`, and sets ownership on the install dir.
3. Enable and start the service: `systemctl enable --now dicorina`

The unit runs the `dicorina` console script, which reads `DICORINA_CONFIG` and binds uvicorn to
`http.bind_host`/`http.bind_port` from the config. One process, two listeners: uvicorn serves
HTTP on the configured port; the pynetdicom DIMSE AE (C-FIND/C-MOVE/C-ECHO) binds
`dimse.listen_ip`/`dimse.listen_port` inside the lifespan; eviction and the healthcheck run as
in-process asyncio tasks.

**`dimsechord` dependency:** `install.sh` runs `uv sync` expecting `../dimsechord` to exist as a
sibling directory (the path source in `pyproject.toml`). On a deploy host without the sibling
repo, build a wheel (`uv build ../dimsechord`), add it to the project, or publish `dimsechord` to
a private index and update `[tool.uv.sources]` accordingly.

**DIMSE port firewall:** the HTTP listener binds `http.bind_host` (default `127.0.0.1`). The
DIMSE face binds `dimse.listen_ip` (default `0.0.0.0`) and must be reachable from the PACS.
Restrict access with a host-level IP-allowlist, for example:

```bash
ufw allow from <PACS_IP> to any port <dimse.listen_port>
```

## Auth

The HTTP API (`/dicom-web/*`) ships open by default (`auth_token = ""`). The HTTP face binds `127.0.0.1` and sits behind Clarinet's nginx reverse proxy (same-origin OHIF). To enforce token authentication, set `DICORINA_AUTH_TOKEN` environment variable or configure `[http] auth_token` in config. When enabled, requests must include either `Authorization: Bearer <token>` or `X-Internal-Token: <token>` header; both use constant-time comparison. Browsers cannot add custom headers, so when authentication is enforced, nginx must inject `X-Internal-Token` on proxied `/dicom-web/` requests (or upgrade to nginx `auth_request` + Clarinet `/api/auth/me`). The DIMSE face (C-MOVE/C-GET/C-FIND) is always protected independently by firewall IP-allowlist, called-AET check, and AET allowlist, regardless of HTTP token setting.
