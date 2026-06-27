"""`dicorina` console entrypoint: run uvicorn with the configured bind."""

from __future__ import annotations

import os

import uvicorn

from dicorina.config import load_config


def main() -> None:
    cfg = load_config(os.environ.get("DICORINA_CONFIG", "config.toml"))
    os.environ.setdefault("DICORINA_CONFIG", os.environ.get("DICORINA_CONFIG", "config.toml"))
    uvicorn.run(
        "dicorina.asgi:app",
        host=cfg.http.bind_host,
        port=cfg.http.bind_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
