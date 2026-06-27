from __future__ import annotations

import pytest

from dicorina.app import create_app
from dicorina.config import load_config


@pytest.mark.asyncio
async def test_app_boots_from_example_config(tmp_path, fake_pacs, free_port) -> None:
    """The shipped example config validates and the app's lifespan starts cleanly."""
    import httpx

    scp_port = free_port()
    cfg_text = f"""
[pacs]
host = "127.0.0.1"
port = {fake_pacs.port}
aet = "{fake_pacs.aet}"

[pool]
aets = ["DICORINA"]

[scp]
bind_ip = "127.0.0.1"
port = {scp_port}

[dimse]
listen_ip = "127.0.0.1"
listen_port = {free_port()}

[http]
bind_host = "127.0.0.1"
bind_port = {free_port()}

[cache]
dir = "{tmp_path / "cache"}"

[healthcheck]
interval_seconds = 9999.0
"""
    cfg_file = tmp_path / "dicorina.toml"
    cfg_file.write_text(cfg_text, encoding="utf-8")

    app = create_app(load_config(cfg_file))
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        assert (await c.get("/health")).status_code == 200
        assert app.state.dimse.is_running
