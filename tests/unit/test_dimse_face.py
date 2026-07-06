"""Unit tests for DimseFace._on_find: it must honour cfind_timeout and release
its upstream find lease deterministically on SCU disconnect -- the DIMSE-side
siblings of ProxyService's HTTP pass-through (see tests/unit/test_service.py)."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from pydicom import Dataset
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    StudyRootQueryRetrieveInformationModelFind,
)

from dicorina.dimse_face.face import DimseFace
from tests.factories import make_instance


def _event(identifier: Dataset, model: object) -> Any:
    return SimpleNamespace(
        identifier=identifier,
        is_cancelled=False,
        context=SimpleNamespace(abstract_syntax=model),
    )


def _face(query: Any, *, loop: Any = None, cfind_timeout: float = 30.0) -> DimseFace:
    none: Any = None
    return DimseFace(none, none, query, none, none, loop, "DICORINA", cfind_timeout=cfind_timeout)


def test_on_find_passes_cfind_timeout() -> None:
    """The pass-through C-FIND must apply the configured cfind timeout, like the
    QIDO path -- otherwise a hung PACS pins a pynetdicom worker thread until
    iter_find's internal default rather than cfg.timeouts.cfind."""
    captured: dict[str, object] = {}

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        captured["timeout"] = timeout
        yield make_instance("1.1", "1.2", "1.3")

    face = _face(SimpleNamespace(iter_find=fake_iter_find), cfind_timeout=17.0)
    list(face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind)))

    assert captured["timeout"] == 17.0


def test_on_find_closes_upstream_deterministically() -> None:
    """On SCU abort/disconnect (GeneratorExit into the paused handler) the upstream
    iter_find generator must be closed via try/finally, not left to GC: a genuine
    reference cycle (as in pynetdicom Association objects) leaves refcounting-based
    cleanup arbitrarily delayed, so without the fix a dropped C-FIND would not
    release its find lease -- exhausting the per-AET find pool."""
    closed: list[str] = []

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        container: list = []

        def gen():
            _keep_cycle_alive = container  # frame -> container
            try:
                for i in range(3):
                    yield make_instance(f"1.{i}", f"2.{i}", f"3.{i}")
            finally:
                closed.append("closed")

        g = gen()
        container.append(g)  # container -> generator, closing the cycle
        return g

    face = _face(SimpleNamespace(iter_find=fake_iter_find))
    handler: Any = face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind))

    gc.disable()
    try:
        next(handler)  # consume the first (0xFF00, ds); upstream mid-iteration
        handler.close()  # SCU disconnect → GeneratorExit into the paused handler
        assert closed == ["closed"]
    finally:
        gc.enable()


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_run_propagates_coroutine_timeout_verbatim(running_loop) -> None:
    """A TimeoutError raised inside the coroutine keeps its message; only the
    future.result wall-clock timeout gets relabelled."""
    face = _face(None, loop=running_loop)

    async def _boom() -> None:
        raise TimeoutError("association timed out")

    with pytest.raises(TimeoutError) as excinfo:
        face._run(_boom())

    assert str(excinfo.value) == "association timed out"
    assert "wall-clock" not in str(excinfo.value)


def test_run_relabels_wall_clock_timeout(running_loop) -> None:
    """future.result timing out while the coroutine still runs must not surface a
    TimeoutError whose str() is empty (the undiagnosable production failure)."""
    face = _face(None, loop=running_loop)
    face._cfind_timeout = -4.9  # wall_clock = cfind_timeout + 5.0 ≈ 0.1s

    release = asyncio.Event()

    async def _hang() -> None:
        await release.wait()

    with pytest.raises(TimeoutError) as excinfo:
        face._run(_hang())

    running_loop.call_soon_threadsafe(release.set)  # let the coroutine finish cleanly
    assert "wall-clock" in str(excinfo.value)


def test_on_find_bare_timeout_logs_type_and_context(caplog) -> None:
    """A bare TimeoutError from upstream must not produce an empty
    'DIMSE C-FIND failed:' log line — it must carry type + query context."""

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        raise TimeoutError  # empty str() — the production failure mode
        yield  # unreachable; makes this a generator like the real iter_find

    ident = Dataset()
    ident.QueryRetrieveLevel = "STUDY"
    ident.StudyInstanceUID = "1.2.3.4"

    face = _face(SimpleNamespace(iter_find=fake_iter_find))
    with caplog.at_level(logging.ERROR, logger="dicorina.dimse_face.face"):
        out = list(face._on_find(_event(ident, StudyRootQueryRetrieveInformationModelFind)))

    assert out == [(0xC000, None)]
    assert caplog.records, "expected an error log record"
    record = caplog.records[0]
    msg = record.getMessage()
    assert "TimeoutError" in msg
    assert "level=STUDY" in msg
    assert "1.2.3.4" in msg
    assert record.exc_info is not None  # logger.exception attaches the traceback


def test_face_ae_requests_compressed_storage_contexts() -> None:
    """The forwarding SCU must propose per-(SOP class x compressed TS) contexts,
    not pynetdicom's uncompressed-only defaults — otherwise every compressed
    instance fails its C-STORE sub-operation at the C-MOVE destination
    (observed live: Completed=5, Failed=807 on a JPEG Lossless series)."""
    from dimsechord import DEFAULT_COMPRESSED_TRANSFER_SYNTAXES

    from dicorina.dimse_face.face import _build_ae

    ae = _build_ae("DICORINA")
    contexts = ae.requested_contexts
    assert 0 < len(contexts) <= 128

    ct = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
    ct_contexts = [cx for cx in contexts if cx.abstract_syntax == ct]

    # One single-TS context per compressed transfer syntax.
    single_ts = {
        cx.transfer_syntax[0] for cx in ct_contexts if len(cx.transfer_syntax) == 1
    }
    assert set(DEFAULT_COMPRESSED_TRANSFER_SYNTAXES) <= single_ts

    # Uncompressed traffic stays covered (Explicit VR LE present in a
    # multi-TS context; pynetdicom interconverts uncompressed TS on send).
    assert any(
        "1.2.840.10008.1.2.1" in cx.transfer_syntax
        for cx in ct_contexts
        if len(cx.transfer_syntax) > 1
    )
