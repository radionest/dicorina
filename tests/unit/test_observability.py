"""Unit tests for the diagnostic instrumentation: the logging setup that makes
records reach the journal at all, and the load snapshot that names the limit a
stalled proxy has hit."""

from __future__ import annotations

import asyncio
import io
import logging
import logging.config
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
        assert counter.snapshot()["find"] == (2, 2)
    assert counter.snapshot()["find"] == (0, 2)


def _fake_face(*, live: int, maximum: int, sessions: int = 0) -> Any:
    counter = InflightCounter()
    return SimpleNamespace(
        association_load=lambda: (live, maximum),
        inflight=counter,
        store_session_count=lambda: sessions,
    )


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
    assert executor_load(loop) == "0/0"  # no to_thread call has built one yet

    await asyncio.to_thread(lambda: None)
    queued, _, workers = executor_load(loop).partition("/")
    assert queued == "0"
    assert int(workers) > 0
