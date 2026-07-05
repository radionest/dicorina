import pytest
from dimsechord import FindFailedError, PoolExhaustedError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dicorina.errors import register_exception_handlers


def _app_raising(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return app


@pytest.mark.asyncio
async def test_pool_exhausted_maps_to_503_with_retry_after() -> None:
    app = _app_raising(PoolExhaustedError("busy"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/boom")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "1"


@pytest.mark.asyncio
async def test_find_failed_maps_to_502() -> None:
    app = _app_raising(FindFailedError(0xA700))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/boom")
    assert r.status_code == 502
