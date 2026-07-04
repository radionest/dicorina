"""Unit test for ProxyService._metadata_stream's deterministic upstream close (see
_qido_stream's `finally: gen.close()` sibling in tests/integration/test_qido.py)."""

from __future__ import annotations

import gc

import pytest

from dicorina.http_face import service as service_mod
from dicorina.http_face.service import ProxyService
from tests.factories import make_instance


def _cyclic_upstream_iter(closed: list[str]):
    """A generator that mimics a real PullEngine/Association iterator: its frame
    is part of a genuine reference cycle (container -> generator -> frame ->
    container), like pynetdicom's Association/event-handler objects. Plain
    refcounting can't reclaim such a cycle promptly -- only an explicit
    `.close()` (our fix) or an eventual cyclic-GC pass closes it."""
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


@pytest.mark.asyncio
async def test_metadata_stream_closes_upstream_iterator_deterministically(monkeypatch) -> None:
    """chunks() must close its upstream iterator via an explicit try/finally, not
    rely on GC: a genuine reference cycle (as in real PullEngine/Association
    objects) leaves refcounting-based cleanup arbitrarily delayed, so without the
    fix a client disconnect would not deterministically release a C-MOVE lease."""
    closed: list[str] = []
    captured: dict[str, object] = {}

    async def fake_iter_to_aiter(make_chunks):
        # Bypasses dimsechord's real thread/queue bridge so the test drives
        # chunks() (the code under test) directly and synchronously, without
        # depending on asyncio's async-generator finalizer timing.
        gen = make_chunks()
        captured["gen"] = gen
        for item in gen:
            yield item

    monkeypatch.setattr(service_mod, "iter_to_aiter", fake_iter_to_aiter)

    service = ProxyService(None, None, None, None, None, None)  # type: ignore[arg-type]
    agen = service._metadata_stream(lambda: _cyclic_upstream_iter(closed), "http://test")

    gc.disable()
    try:
        ait = agen.__aiter__()
        await ait.__anext__()  # consume only the first chunk
        captured["gen"].close()  # mimic iter_to_aiter's producer-thread `gen.close()`
        assert closed == ["closed"]
    finally:
        gc.enable()
