"""Unit tests for the diagnostic instrumentation: the logging setup that makes
records reach the journal at all, and the load snapshot that names the limit a
stalled proxy has hit."""

from __future__ import annotations

import asyncio
import io
import logging
import logging.config
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from uvicorn.config import LOGGING_CONFIG

from dicorina.config import LoggingConfig
from dicorina.logging_setup import configure_logging
from dicorina.stats import InflightCounter, LoadSnapshotLoop, executor_load


@pytest.fixture(autouse=True)
def isolated_logging():
    """Snapshot and restore global logging state around a test."""
    root = logging.getLogger()
    handlers, root_level = list(root.handlers), root.level
    names = ("dicorina", "dimsechord", "pynetdicom")
    levels = {name: logging.getLogger(name).level for name in names}
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def _capture() -> io.StringIO:
    """Redirect the installed dicorina handler into a buffer."""
    installed = [h for h in logging.getLogger().handlers if h.get_name() == "dicorina"]
    assert len(installed) == 1, f"expected exactly one dicorina handler, got {installed}"
    stream = io.StringIO()
    installed[0].setStream(stream)  # type: ignore[attr-defined]
    return stream


def test_info_records_reach_the_root_handler() -> None:
    """The production failure this closes: with no root handler, dicorina's INFO
    records fell through to logging.lastResort (WARNING) and never reached
    journald, so an overloaded proxy refused clients in silence."""
    configure_logging(LoggingConfig())
    stream = _capture()

    logging.getLogger("dicorina.probe").info("association accepted")
    logging.getLogger("dimsechord.probe").info("lease acquired")

    written = stream.getvalue()
    assert "association accepted" in written
    assert "lease acquired" in written


def test_uvicorn_logging_config_alone_drops_info() -> None:
    """Guards the premise: uvicorn's own config binds handlers to the uvicorn*
    loggers only and leaves dicorina's tree with none of its own."""
    logging.getLogger().handlers[:] = []
    logging.config.dictConfig(LOGGING_CONFIG)

    assert logging.getLogger().handlers == []
    assert logging.getLogger("uvicorn").handlers != []


def test_configure_logging_is_idempotent() -> None:
    """Both entrypoints call it — __main__ before uvicorn starts and asgi at
    import time — so the second call must replace, not duplicate."""
    configure_logging(LoggingConfig())
    configure_logging(LoggingConfig())

    installed = [h for h in logging.getLogger().handlers if h.get_name() == "dicorina"]
    assert len(installed) == 1


def test_survives_uvicorn_reconfiguring_logging_afterwards() -> None:
    """uvicorn runs its dictConfig between main() and importing dicorina.asgi;
    that must not strip the root handler installed before it."""
    configure_logging(LoggingConfig())
    logging.config.dictConfig(LOGGING_CONFIG)

    installed = [h for h in logging.getLogger().handlers if h.get_name() == "dicorina"]
    assert len(installed) == 1
    assert logging.getLogger("dicorina.probe").isEnabledFor(logging.INFO)


def test_pynetdicom_level_is_independent() -> None:
    """pynetdicom at INFO is what surfaces its 'Rejecting Association' record,
    but at DEBUG it dumps every PDU — hence the separate knob."""
    configure_logging(LoggingConfig(level="INFO"))
    assert not logging.getLogger("pynetdicom").isEnabledFor(logging.INFO)

    configure_logging(LoggingConfig(level="INFO", pynetdicom_level="INFO"))
    stream = _capture()
    logging.getLogger("pynetdicom").info("Rejecting Association")
    assert "Rejecting Association" in stream.getvalue()


def test_inflight_counter_tracks_current_and_peak() -> None:
    counter = InflightCounter()
    with counter.track("find"), counter.track("find"):
        now, peak, _ = counter.snapshot()["find"]
        assert (now, peak) == (2, 2)
    now, peak, oldest = counter.snapshot()["find"]
    assert (now, peak, oldest) == (0, 2, 0.0)


def test_inflight_counter_ages_the_oldest_operation() -> None:
    """An operation that never returns never logs a completion line — its only
    trace is sitting in the counter getting older while it holds a slot."""
    counter = InflightCounter()
    with counter.track("find"):
        time.sleep(0.02)
        with counter.track("find"):
            now, _, oldest = counter.snapshot()["find"]
            assert now == 2
            assert oldest >= 0.02  # the age of the OUTER one, not the newest


def test_inflight_counter_is_concurrency_safe() -> None:
    """track() runs on pynetdicom worker threads, several at once."""
    counter = InflightCounter()
    barrier = threading.Barrier(9)

    def worker() -> None:
        with counter.track("store"):
            barrier.wait(timeout=5)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    barrier.wait(timeout=5)
    for t in threads:
        t.join(timeout=5)

    now, peak, _ = counter.snapshot()["store"]
    assert (now, peak) == (0, 8)


def _fake_face(*, live: int, maximum: int, sessions: int = 0) -> Any:
    counter = InflightCounter()
    return SimpleNamespace(
        association_load=lambda: (live, maximum),
        inflight=counter,
        store_session_count=lambda: sessions,
    )


async def test_snapshot_warns_on_an_operation_stuck_past_the_slow_threshold(caplog) -> None:
    """The failure this PR exists for: an operation that holds its association
    slot and never finishes, so it never logs a completion line either."""
    face = _fake_face(live=3, maximum=10)
    loop = LoadSnapshotLoop(face, slow_operation_seconds=0.01)

    with face.inflight.track("find"):
        await asyncio.sleep(0.02)
        with caplog.at_level(logging.INFO, logger="dicorina.stats"):
            loop.log_once()

    assert caplog.records[0].levelno == logging.WARNING
    assert "find in flight past slow_operation_seconds" in caplog.text
    assert "oldest" in caplog.text


async def test_snapshot_stays_at_info_while_operations_are_young(caplog) -> None:
    face = _fake_face(live=3, maximum=10)
    loop = LoadSnapshotLoop(face, slow_operation_seconds=60.0)

    with face.inflight.track("find"), caplog.at_level(logging.INFO, logger="dicorina.stats"):
        loop.log_once()

    assert caplog.records[0].levelno == logging.INFO
    assert "find=1(peak 1, oldest" in caplog.text


async def test_snapshot_loop_survives_a_failing_log_once(caplog) -> None:
    """A monitor that dies is silent, which is the failure class this removes."""
    face = _fake_face(live=1, maximum=10)
    face.association_load = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    loop = LoadSnapshotLoop(face, interval_seconds=0.01)

    with caplog.at_level(logging.ERROR, logger="dicorina.stats"):
        loop.start()
        await asyncio.sleep(0.05)
        still_running = loop._task is not None and not loop._task.done()
        loop.stop()

    assert still_running, "the loop must outlive an exception from log_once"
    assert "Load snapshot failed" in caplog.text


async def test_snapshot_logs_load_at_info(caplog) -> None:
    loop = LoadSnapshotLoop(_fake_face(live=2, maximum=10, sessions=3))
    with caplog.at_level(logging.INFO, logger="dicorina.stats"):
        loop.log_once()

    assert "dimse_assoc=2/10" in caplog.text
    assert "store_sessions=3" in caplog.text
    assert caplog.records[0].levelno == logging.INFO


async def test_snapshot_warns_when_association_limit_reached(caplog) -> None:
    """At the cap pynetdicom refuses every further request above dicorina's
    handlers, so nothing else in the process would report it."""
    loop = LoadSnapshotLoop(_fake_face(live=10, maximum=10))
    with caplog.at_level(logging.INFO, logger="dicorina.stats"):
        loop.log_once()

    assert caplog.records[0].levelno == logging.WARNING
    assert "association limit reached" in caplog.text


async def test_snapshot_warns_on_event_loop_lag(caplog) -> None:
    loop = LoadSnapshotLoop(_fake_face(live=1, maximum=10))
    with caplog.at_level(logging.INFO, logger="dicorina.stats"):
        loop.log_once(lag_ms=4000.0)

    assert caplog.records[0].levelno == logging.WARNING
    assert "event loop blocked" in caplog.text


async def test_executor_load_reports_worker_saturation() -> None:
    loop = asyncio.get_running_loop()
    # Distinct from "0/0", which would read as evidence the executor is idle.
    assert executor_load(loop) == "none"

    await asyncio.to_thread(lambda: None)
    queued, _, workers = executor_load(loop).partition("/")
    assert queued == "0"
    assert int(workers) > 0
