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
from dimsechord import SeriesResult
from pydicom import Dataset
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    StudyRootQueryRetrieveInformationModelFind,
)

from dicorina.dimse_face.face import DimseFace, _peer
from tests.factories import make_instance


def _event(identifier: Dataset, model: object) -> Any:
    return SimpleNamespace(
        identifier=identifier,
        is_cancelled=False,
        context=SimpleNamespace(abstract_syntax=model),
        assoc=object(),
    )


def _ae(live: int, maximum: int) -> Any:
    return SimpleNamespace(
        active_associations=[SimpleNamespace(is_acceptor=True) for _ in range(live)],
        maximum_associations=maximum,
    )


def _end_event(assoc: object, name: str = "EVT_RELEASED") -> Any:
    return SimpleNamespace(assoc=assoc, event=SimpleNamespace(name=name))


def _face(
    query: Any,
    *,
    engine: Any = None,
    client: Any = None,
    loop: Any = None,
    cfind_timeout: float = 30.0,
    slow_operation_seconds: float = 10.0,
) -> DimseFace:
    none: Any = None
    return DimseFace(
        engine,
        client,
        query,
        none,
        none,
        loop,
        "DICORINA",
        cfind_timeout=cfind_timeout,
        slow_operation_seconds=slow_operation_seconds,
    )


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


def test_peer_label_reads_service_user_info() -> None:
    """pynetdicom exposes ServiceUser.info as a property; calling it degraded
    every association log line to the useless 'unknown-peer' placeholder."""

    class _Requestor:
        @property
        def info(self) -> dict[str, Any]:
            return {
                "ae_title": "CLIENT",
                "address": "10.0.0.9",
                "port": 4321,
                "mode": "requestor",
            }

    assert _peer(SimpleNamespace(requestor=_Requestor())) == "CLIENT@10.0.0.9:4321"


def test_peer_label_never_raises_on_an_incomplete_association() -> None:
    """Log context must not be the reason a DIMSE handler fails."""
    assert _peer(SimpleNamespace()) == "unknown-peer"


def test_association_accept_and_end_are_logged(caplog) -> None:
    """Association churn is invisible in the journal otherwise: pynetdicom logs
    accept/reject at INFO on its own logger and dicorina logged neither, so a
    face that had stopped taking clients looked identical to an idle one."""
    face = _face(None)
    face._ae = _ae(1, 10)
    assoc = object()

    with caplog.at_level(logging.INFO, logger="dicorina.dimse_face.face"):
        face._on_accepted(SimpleNamespace(assoc=assoc))
        face._bump(assoc, "find")
        face._log_assoc_end(_end_event(assoc))

    assert "association accepted" in caplog.text
    assert "1/10 slots in use" in caplog.text
    assert "association released" in caplog.text
    assert "find=1" in caplog.text


def test_association_end_is_summarised_once(caplog) -> None:
    """RELEASED, ABORTED and CONN_CLOSE can all fire for one association."""
    face = _face(None)
    face._ae = _ae(1, 10)
    assoc = object()
    face._on_accepted(SimpleNamespace(assoc=assoc))

    with caplog.at_level(logging.INFO, logger="dicorina.dimse_face.face"):
        face._log_assoc_end(_end_event(assoc, "EVT_RELEASED"))
        face._log_assoc_end(_end_event(assoc, "EVT_CONN_CLOSE"))

    assert len([r for r in caplog.records if "association released" in r.getMessage()]) == 1
    assert "conn_close" not in caplog.text


def test_association_limit_warns_on_accept(caplog) -> None:
    """At the cap pynetdicom refuses every further request before any handler of
    ours runs, so this is the last point the proxy can report it."""
    face = _face(None)
    face._ae = _ae(10, 10)

    with caplog.at_level(logging.INFO, logger="dicorina.dimse_face.face"):
        face._on_accepted(SimpleNamespace(assoc=object()))

    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "association limit reached" in caplog.text


def test_rejection_logs_decoded_reason(caplog) -> None:
    """pynetdicom announces its rejection as a bare 'Rejecting Association' at
    INFO, without the reason — this line carries it."""
    face = _face(None)
    face._ae = _ae(10, 10)
    assoc = SimpleNamespace(
        acceptor=SimpleNamespace(
            primitive=SimpleNamespace(result=0x02, result_source=0x03, diagnostic=0x02)
        ),
        requestor=SimpleNamespace(primitive=SimpleNamespace(called_ae_title="DICORINA")),
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        face._on_rejected(SimpleNamespace(assoc=assoc))

    assert "association REJECTED" in caplog.text
    assert "local limit exceeded" in caplog.text
    assert "10/10" in caplog.text


def test_find_completion_logs_match_count(caplog) -> None:
    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        yield make_instance("1.1", "1.2", "1.3")
        yield make_instance("1.1", "1.2", "1.4")

    face = _face(SimpleNamespace(iter_find=fake_iter_find))
    with caplog.at_level(logging.INFO, logger="dicorina.dimse_face.face"):
        list(face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind)))

    assert "C-FIND ok" in caplog.text
    assert "matched=2" in caplog.text


def test_slow_find_is_warned(caplog) -> None:
    """A slow operation holds its SCP worker thread and its association slot for
    the whole duration, so it must stand out from the ordinary per-op line."""

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        yield make_instance("1.1", "1.2", "1.3")

    face = _face(SimpleNamespace(iter_find=fake_iter_find), slow_operation_seconds=0.0)
    with caplog.at_level(logging.INFO, logger="dicorina.dimse_face.face"):
        list(face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind)))

    assert any(
        r.levelno == logging.WARNING and "C-FIND" in r.getMessage() for r in caplog.records
    )


def test_find_dropped_by_client_is_not_logged_as_ok(caplog) -> None:
    """An SCU that disconnects mid-query must be distinguishable from a query
    that ran to completion — both end the generator, only one succeeded."""

    def fake_iter_find(identifier, *, model, timeout=None):  # noqa: ARG001
        for i in range(3):
            yield make_instance(f"1.{i}", f"2.{i}", f"3.{i}")

    face = _face(SimpleNamespace(iter_find=fake_iter_find))
    handler: Any = face._on_find(_event(Dataset(), StudyRootQueryRetrieveInformationModelFind))

    with caplog.at_level(logging.INFO, logger="dicorina.dimse_face.face"):
        next(handler)
        handler.close()

    assert "C-FIND incomplete" in caplog.text
    assert "matched=1" in caplog.text


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


def _series_result(study: str, series: str, n: int) -> SeriesResult:
    return SeriesResult(
        study_instance_uid=study,
        series_instance_uid=series,
        number_of_series_related_instances=n,
    )


def test_series_move_counts_only_requested_series(running_loop, caplog) -> None:
    """A backend that ignores the SeriesInstanceUID matching key returns one result
    per series in the study; only the requested series may be counted (#21)."""
    results = [_series_result("1.2", "1.2.1", 2), _series_result("1.2", "1.2.9", 3)]

    async def find_series(query, peer, timeout=30.0):  # noqa: ARG001, ASYNC109
        return results

    sentinel = iter(())
    face = _face(
        None,
        engine=SimpleNamespace(iter_series=lambda *_a: sentinel),
        client=SimpleNamespace(find_series=find_series),
        loop=running_loop,
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        count, iterator = face._series_move("1.2", "1.2.1")

    assert count == 2
    assert iterator is sentinel
    assert "SeriesInstanceUID matching key" in caplog.text


def test_series_move_no_warning_when_backend_conformant(running_loop, caplog) -> None:
    results = [_series_result("1.2", "1.2.1", 4)]

    async def find_series(query, peer, timeout=30.0):  # noqa: ARG001, ASYNC109
        return results

    face = _face(
        None,
        engine=SimpleNamespace(iter_series=lambda *_a: iter(())),
        client=SimpleNamespace(find_series=find_series),
        loop=running_loop,
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        count, _ = face._series_move("1.2", "1.2.1")

    assert count == 4
    assert "matching key" not in caplog.text


def test_study_move_filters_foreign_study_results(running_loop, caplog) -> None:
    """Foreign-study rows from a match-widening backend must not leak into the
    sub-operation count nor into the series list handed to iter_study."""
    results = [_series_result("1.2", "1.2.1", 2), _series_result("9.9", "9.9.1", 7)]

    async def find_series(query, peer, timeout=30.0):  # noqa: ARG001, ASYNC109
        return results

    captured: dict[str, Any] = {}

    def iter_study(study, series_uids):
        captured["args"] = (study, series_uids)
        return iter(())

    face = _face(
        None,
        engine=SimpleNamespace(iter_study=iter_study),
        client=SimpleNamespace(find_series=find_series),
        loop=running_loop,
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        count, _ = face._study_move("1.2")

    assert count == 2
    assert captured["args"] == ("1.2", ["1.2.1"])
    assert "StudyInstanceUID matching key" in caplog.text


def test_series_move_falls_back_when_no_result_matches(running_loop, caplog) -> None:
    """A backend that widens the match AND does not echo the requested UID must not
    produce count=0 while instances still stream -- fall back to the unfiltered total."""
    results = [_series_result("1.2", "1.2.9", 3)]

    async def find_series(query, peer, timeout=30.0):  # noqa: ARG001, ASYNC109
        return results

    face = _face(
        None,
        engine=SimpleNamespace(iter_series=lambda *_a: iter(())),
        client=SimpleNamespace(find_series=find_series),
        loop=running_loop,
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        count, _ = face._series_move("1.2", "1.2.1")

    assert count == 3
    assert "falling back to the unfiltered total" in caplog.text


def test_study_move_falls_back_when_no_result_matches(running_loop, caplog) -> None:
    results = [_series_result("9.9", "9.9.1", 7)]

    async def find_series(query, peer, timeout=30.0):  # noqa: ARG001, ASYNC109
        return results

    captured: dict[str, Any] = {}

    def iter_study(study, series_uids):
        captured["args"] = (study, series_uids)
        return iter(())

    face = _face(
        None,
        engine=SimpleNamespace(iter_study=iter_study),
        client=SimpleNamespace(find_series=find_series),
        loop=running_loop,
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        count, _ = face._study_move("1.2")

    assert count == 7
    assert captured["args"] == ("1.2", ["9.9.1"])
    assert "falling back to the unfiltered total" in caplog.text


def test_nonconformant_warning_logged_once_per_key(running_loop, caplog) -> None:
    """Repeat offenses by the same backend must not spam WARNING on every C-MOVE."""
    results = [_series_result("1.2", "1.2.1", 2), _series_result("1.2", "1.2.9", 3)]

    async def find_series(query, peer, timeout=30.0):  # noqa: ARG001, ASYNC109
        return results

    face = _face(
        None,
        engine=SimpleNamespace(iter_series=lambda *_a: iter(())),
        client=SimpleNamespace(find_series=find_series),
        loop=running_loop,
    )

    with caplog.at_level(logging.WARNING, logger="dicorina.dimse_face.face"):
        face._series_move("1.2", "1.2.1")
        face._series_move("1.2", "1.2.1")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
