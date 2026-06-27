# dicorina

Pure-Python DICOM + DICOMweb pass-through proxy for a C-MOVE-only PACS, built as a thin application layer over the prebuilt `dimsechord` core library (sourced from `../dimsechord` via editable path install). To develop, install dependencies and run tests: `uv sync && uv run pytest`.

## Auth

The HTTP API (`/dicom-web/*`) ships open by default (`auth_token = ""`). The HTTP face binds `127.0.0.1` and sits behind Clarinet's nginx reverse proxy (same-origin OHIF). To enforce token authentication, set `DICORINA_AUTH_TOKEN` environment variable or configure `[http] auth_token` in config. When enabled, requests must include either `Authorization: Bearer <token>` or `X-Internal-Token: <token>` header; both use constant-time comparison. Browsers cannot add custom headers, so when authentication is enforced, nginx must inject `X-Internal-Token` on proxied `/dicom-web/` requests (or upgrade to nginx `auth_request` + Clarinet `/api/auth/me`). The DIMSE face (C-MOVE/C-GET/C-FIND) is always protected independently by firewall IP-allowlist, called-AET check, and AET allowlist, regardless of HTTP token setting.
