from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_endpoint_up(app_client) -> None:
    client, _ctx = app_client
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"starting", "ok", "degraded"}


@pytest.mark.asyncio
async def test_core_is_built_on_state(app_client) -> None:
    _client, ctx = app_client
    app = ctx["app"]
    # The shared dimsechord core was constructed during lifespan startup.
    assert app.state.cache is not None
    assert app.state.engine is not None
    assert app.state.pool.aets == [ctx["pool_aet"]]
