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
    QueryEngine,
    StorageSCP,
)
from fastapi import FastAPI

from dicorina.errors import register_exception_handlers
from dicorina.eviction import EvictionLoop

if TYPE_CHECKING:
    from dicorina.config import DicorinaConfig


def _configure_pydicom() -> None:
    # We relay datasets from upstream PACS verbatim; their VR violations are not ours
    # to fix and otherwise flood the journal with one pydicom warning per result.
    # Process-wide setting; each uvicorn worker runs its own lifespan, so it is
    # applied once per worker.
    import pydicom.config

    pydicom.config.settings.reading_validation_mode = pydicom.config.IGNORE
    # Some upstream (Philips) private elements carry a value whose byte length is
    # incompatible with the VR pydicom infers (e.g. (01F1,1026) = b'1.127 ', 6 bytes
    # read as an 8-byte double). On re-encode for the C-MOVE forward, pydicom would
    # raise BytesLengthException and the whole C-STORE sub-operation fails — silently
    # dropping every instance that carries the element. Coerce such elements to UN so
    # the dataset relays verbatim instead of failing to encode.
    pydicom.config.convert_wrong_length_to_UN = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_pydicom()
    cfg: DicorinaConfig = app.state.config

    members = cfg.pool.members
    pool = AssociationPool(
        [m.aet for m in members], cfg.pool.per_aet_cap, cfg.pool.per_aet_find_cap
    )
    scp = StorageSCP()
    scp.start({m.aet: m.port for m in members}, cfg.scp.bind_ip)
    cache = DicomCache(
        cfg.cache.dir,
        ttl_hours=cfg.cache.disk_ttl_hours,
        max_size_gb=cfg.cache.disk_max_size_gb,
        memory_ttl_minutes=cfg.cache.memory_ttl_minutes,
        memory_max_size_gb=cfg.cache.memory_max_size_gb,
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

    query = QueryEngine(
        pool, pacs, find_timeout=cfg.timeouts.cfind, lease_timeout=cfg.timeouts.find_lease
    )
    app.state.query = query

    from dicorina.http_face.qido_cache import QidoResultCache
    from dicorina.http_face.service import ProxyService

    qido_cache = QidoResultCache(cfg.cache.qido_ttl_seconds)

    app.state.pool = pool
    app.state.scp = scp
    app.state.cache = cache
    app.state.pacs = pacs
    app.state.engine = engine
    app.state.client = client
    app.state.loop = asyncio.get_running_loop()
    app.state.service = ProxyService(
        client, engine, cache, pacs, qido_cache, query, cfind_timeout=cfg.timeouts.cfind
    )

    from dicorina.healthcheck import Healthcheck

    health = Healthcheck(pacs, engine, cfg.healthcheck, primary_aet=pool.aets[0])
    await health.startup()
    health.start()
    app.state.health = health

    from dicorina.dimse_face.allowlist import DestinationAllowlist
    from dicorina.dimse_face.face import DimseFace

    dimse = DimseFace(
        engine=engine,
        client=client,
        query=query,
        pacs=pacs,
        allowlist=DestinationAllowlist(cfg.dimse.allowlist),
        loop=app.state.loop,
        aet=cfg.dimse.aet,
        cfind_timeout=cfg.timeouts.cfind,
        store_aet=cfg.pacs.store_aet,
        store_timeout=cfg.timeouts.store,
    )
    dimse.start(cfg.dimse.listen_port, cfg.dimse.listen_ip)
    app.state.dimse = dimse

    eviction = EvictionLoop(cache, cfg.cache.eviction_interval_seconds)
    eviction.start()
    app.state.eviction = eviction

    try:
        yield
    finally:
        health.stop()
        eviction.stop()
        dimse.stop()
        scp.stop()
        cache.shutdown()


def create_app(config: DicorinaConfig) -> FastAPI:
    app = FastAPI(title="dicorina", lifespan=lifespan)
    app.state.config = config
    register_exception_handlers(app)

    from fastapi import Depends

    from dicorina.deps import verify_token
    from dicorina.http_face.qido import router as qido_router
    from dicorina.http_face.wado import router as wado_router

    app.include_router(qido_router, prefix="/dicom-web", dependencies=[Depends(verify_token)])
    app.include_router(wado_router, prefix="/dicom-web", dependencies=[Depends(verify_token)])

    @app.get("/health")
    async def health() -> dict:
        h = app.state.health
        return h if isinstance(h, dict) else h.snapshot()

    if config.ohif.enabled:
        from pathlib import Path

        from fastapi import Request
        from fastapi.responses import Response

        from dicorina.http_face.ohif import inject_datasources, render_datasources_js

        _tpl = (Path(__file__).parent / "http_face" / "app-config.js").read_text(encoding="utf-8")

        @app.get("/ohif/app-config.js")
        async def ohif_config(request: Request) -> Response:
            js = render_datasources_js(
                friendly_name=config.ohif.friendly_name,
                base_path=str(request.scope.get("root_path", "")),
                external_root=config.ohif.external_root,
            )
            rendered = inject_datasources(_tpl, js)
            return Response(rendered or _tpl, media_type="application/javascript")

    return app
