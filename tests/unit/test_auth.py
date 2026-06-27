from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI

from dicorina.config import DicorinaConfig
from dicorina.deps import verify_token


def _app(token: str) -> FastAPI:
    app = FastAPI()
    app.state.config = DicorinaConfig.model_validate(
        {
            "pacs": {"host": "h"},
            "scp": {},
            "cache": {"dir": "/tmp/x"},
            "http": {"auth_token": token},
        }
    )

    @app.get("/guarded", dependencies=[Depends(verify_token)])
    async def guarded() -> dict:
        return {"ok": True}

    return app


async def _get(app: FastAPI, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get("/guarded", headers=headers or {})


@pytest.mark.asyncio
async def test_open_when_token_empty() -> None:
    assert (await _get(_app(""))).status_code == 200


@pytest.mark.asyncio
async def test_rejected_without_token_when_required() -> None:
    assert (await _get(_app("s3cret"))).status_code == 401


@pytest.mark.asyncio
async def test_bearer_accepted() -> None:
    r = await _get(_app("s3cret"), {"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_internal_token_header_accepted() -> None:
    r = await _get(_app("s3cret"), {"X-Internal-Token": "s3cret"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_wrong_token_rejected() -> None:
    r = await _get(_app("s3cret"), {"Authorization": "Bearer nope"})
    assert r.status_code == 401
