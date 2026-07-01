from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

import pytest
from pydicom.dataset import Dataset

from dicorina.dimse_face.face import DimseFace

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeClient:
    def find_studies(self, *_args: object, **_kwargs: object) -> object:
        return "coroutine-sentinel"


class _FakeEvent:
    def __init__(self, identifier: Dataset) -> None:
        self.identifier = identifier
        self.is_cancelled = False


def _face() -> DimseFace:
    return DimseFace(
        engine=None,  # type: ignore[arg-type]
        client=_FakeClient(),  # type: ignore[arg-type]
        pacs=None,  # type: ignore[arg-type]
        allowlist=None,  # type: ignore[arg-type]
        loop=None,  # type: ignore[arg-type]
        called_aets=["DICORINA"],
    )


def test_on_find_timeout_logs_informative_message(caplog) -> None:
    """A bare TimeoutError must not produce an empty 'C-FIND failed:' log line."""
    face = _face()

    def _raise(_coro: object) -> object:
        raise TimeoutError  # empty str() — the production failure mode

    face._run = _raise  # type: ignore[method-assign]

    ident = Dataset()
    ident.QueryRetrieveLevel = "STUDY"
    ident.StudyInstanceUID = "1.2.3.4"

    with caplog.at_level(logging.ERROR, logger="dicorina.dimse_face.face"):
        out = list(face._on_find(_FakeEvent(ident)))

    assert out == [(0xC000, None)]
    assert caplog.records, "expected an error log record"
    msg = caplog.records[0].getMessage()
    assert "TimeoutError" in msg
    assert "1.2.3.4" in msg
    assert "level=STUDY" in msg


@pytest.fixture
def running_loop() -> Iterator[asyncio.AbstractEventLoop]:
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
    """A TimeoutError from the coroutine keeps its message; only future.result relabels."""
    face = DimseFace(
        engine=None,  # type: ignore[arg-type]
        client=_FakeClient(),  # type: ignore[arg-type]
        pacs=None,  # type: ignore[arg-type]
        allowlist=None,  # type: ignore[arg-type]
        loop=running_loop,
        called_aets=["DICORINA"],
    )

    async def _boom() -> None:
        raise TimeoutError("association timed out")

    with pytest.raises(TimeoutError) as excinfo:
        face._run(_boom())

    assert str(excinfo.value) == "association timed out"
    assert "wall-clock" not in str(excinfo.value)
