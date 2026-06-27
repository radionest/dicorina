"""FastAPI app factory + lifespan that owns the shared dimsechord core."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from dimsechord import (
    AssociationPool,
    DicomCache,
    DicomClient,
    DicomNode,
    PullEngine,
    StorageSCP,
)
from fastapi import FastAPI

from dicorina.errors import register_exception_handlers

if TYPE_CHECKING:
    from dicorina.config import DicorinaConfig


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: DicorinaConfig = app.state.config

    pool = AssociationPool(cfg.pool.aets, cfg.pool.per_aet_cap)
    scp = StorageSCP()
    scp.start(pool.aets, cfg.scp.port, cfg.scp.bind_ip)
    cache = DicomCache(
        cfg.cache.dir,
        ttl_hours=cfg.cache.disk_ttl_hours,
        max_size_gb=cfg.cache.disk_max_size_gb,
        memory_ttl_minutes=cfg.cache.memory_ttl_minutes,
        memory_max_entries=cfg.cache.memory_max_entries,
    )
    pacs = DicomNode(aet=cfg.pacs.aet, host=cfg.pacs.host, port=cfg.pacs.port)
    engine = PullEngine(
        pool,
        scp,
        cache,
        pacs,
        cmove_timeout=cfg.timeouts.cmove,
        arrival_timeout=cfg.timeouts.arrival,
        completion_grace=cfg.timeouts.completion_grace,
    )
    client = DicomClient(calling_aet=pool.aets[0])

    app.state.pool = pool
    app.state.scp = scp
    app.state.cache = cache
    app.state.pacs = pacs
    app.state.engine = engine
    app.state.client = client
    app.state.loop = asyncio.get_running_loop()
    app.state.service = None  # Task 5 sets ProxyService
    app.state.health = {"status": "starting"}  # Task 10 replaces with Healthcheck

    try:
        yield
    finally:
        scp.stop()
        cache.shutdown()


def create_app(config: DicorinaConfig) -> FastAPI:
    app = FastAPI(title="dicorina", lifespan=lifespan)
    app.state.config = config
    register_exception_handlers(app)

    @app.get("/health")
    async def health() -> dict:
        h = app.state.health
        return h if isinstance(h, dict) else h.snapshot()

    return app
