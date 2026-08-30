"""FastAPI app factory + lifespan that owns the shared dimsechord core."""

from __future__ import annotations

import asyncio
import logging
import time
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

from dicorina import __version__
from dicorina.errors import register_exception_handlers
from dicorina.eviction import EvictionLoop
from dicorina.stats import LoadSnapshotLoop

if TYPE_CHECKING:
    from collections.abc import Callable

    from dicorina.config import DicorinaConfig

logger = logging.getLogger(__name__)


def _timed_stop(name: str, stop: Callable[[], None]) -> None:
    """Run one shutdown step and log how long it took.

    systemd SIGKILLs the process once ``TimeoutStopSec`` elapses; without a
    line per step there is no way to tell afterwards which one ran over.
    """
    started = time.monotonic()
    try:
        stop()
    finally:
        logger.info("shutdown: %s took %.1fs", name, time.monotonic() - started)


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

    logger.info(
        "dicorina %s starting — pacs %s@%s:%s, dimse face %s on %s:%s, http %s:%s",
        __version__,
        cfg.pacs.aet,
        cfg.pacs.host,
        cfg.pacs.port,
        cfg.dimse.aet,
        cfg.dimse.listen_ip,
        cfg.dimse.listen_port,
        cfg.http.bind_host,
        cfg.http.bind_port,
    )
    members = cfg.pool.members
    # The concurrency ceilings and the timeouts that decide how long each slot
    # stays taken: the numbers any load incident has to be read against.
    logger.info(
        "pool: AETs %s, per_aet_cap=%d/AET (%d concurrent C-MOVEs in total), "
        "per_aet_find_cap=%d/AET (%d concurrent C-FINDs in total), "
        "scp max_assoc=%d queue=%d; "
        "timeouts cfind=%.0fs arrival=%.0fs find_lease=%.0fs store=%.0fs move_lease=%.0fs",
        [m.aet for m in members],
        cfg.pool.per_aet_cap,
        cfg.pool.per_aet_cap * len(members),
        cfg.pool.per_aet_find_cap,
        cfg.pool.per_aet_find_cap * len(members),
        cfg.scp.max_associations,
        cfg.scp.session_queue_maxsize,
        cfg.timeouts.cfind,
        cfg.timeouts.arrival,
        cfg.timeouts.find_lease,
        cfg.timeouts.store,
        cfg.timeouts.move_lease,
    )
    pool = AssociationPool(
        [m.aet for m in members], cfg.pool.per_aet_cap, cfg.pool.per_aet_find_cap
    )
    scp = StorageSCP(
        maximum_associations=cfg.scp.max_associations,
        session_queue_maxsize=cfg.scp.session_queue_maxsize,
    )
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
        arrival_timeout=cfg.timeouts.arrival,
        completion_grace=cfg.timeouts.completion_grace,
        move_lease_timeout=cfg.timeouts.move_lease,
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
        slow_operation_seconds=cfg.logging.slow_operation_seconds,
    )
    dimse.start(cfg.dimse.listen_port, cfg.dimse.listen_ip)
    app.state.dimse = dimse

    snapshot = LoadSnapshotLoop(
        dimse,
        interval_seconds=cfg.logging.snapshot_interval_seconds,
        slow_operation_seconds=cfg.logging.slow_operation_seconds,
    )
    snapshot.start()
    app.state.snapshot = snapshot

    eviction = EvictionLoop(cache, cfg.cache.eviction_interval_seconds)
    eviction.start()
    app.state.eviction = eviction

    try:
        yield
    finally:
        logger.info("shutdown: stopping background loops")
        snapshot.stop()
        health.stop()
        eviction.stop()
        _timed_stop("dimse face", dimse.stop)
        _timed_stop("storage scp", scp.stop)
        _timed_stop("cache", cache.shutdown)


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
