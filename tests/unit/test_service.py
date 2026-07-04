"""Unit test for ProxyService._metadata_stream's deterministic upstream close (see
_qido_stream's `finally: gen.close()` sibling in tests/integration/test_qido.py)."""

from __future__ import annotations

import gc

import pytest
from dimsechord import FindFailedError
from pydicom import Dataset

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


async def _drive_sync(make_chunks):
    """Sync-driving stand-in for dimsechord's thread/queue bridge."""
    for item in make_chunks():
        yield item


class _SpyQidoCache:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key: str, body: bytes) -> None:
        self.puts.append((key, body))


@pytest.mark.asyncio
async def test_tee_passes_through_without_buffering_when_cache_disabled(monkeypatch) -> None:
    monkeypatch.setattr(service_mod, "iter_to_aiter", _drive_sync)
    spy = _SpyQidoCache(enabled=False)
    service = ProxyService(None, None, None, None, spy, None)  # type: ignore[arg-type]
    got = [c async for c in service._tee_into_cache(lambda: iter([b"[", b"]"]), "KEY")]
    assert got == [b"[", b"]"]
    assert spy.puts == []


@pytest.mark.asyncio
async def test_tee_caches_complete_body_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(service_mod, "iter_to_aiter", _drive_sync)
    spy = _SpyQidoCache(enabled=True)
    service = ProxyService(None, None, None, None, spy, None)  # type: ignore[arg-type]
    got = [c async for c in service._tee_into_cache(lambda: iter([b"[", b"]"]), "KEY")]
    assert got == [b"[", b"]"]
    assert spy.puts == [("KEY", b"[]")]


class _TwoThenFailQuery:
    def iter_find(self, identifier, *, model, timeout):  # noqa: ARG002
        yield make_instance("1.0", "2.0", "3.0")
        yield make_instance("1.1", "2.1", "3.1")
        raise FindFailedError(0xA700)


@pytest.mark.asyncio
async def test_qido_stream_mid_failure_yields_prefix_and_never_caches(monkeypatch) -> None:
    monkeypatch.setattr(service_mod, "iter_to_aiter", _drive_sync)
    spy = _SpyQidoCache(enabled=True)
    service = ProxyService(None, None, None, None, spy, _TwoThenFailQuery())  # type: ignore[arg-type]
    agen = service._qido_stream(Dataset(), "KEY", None, 0)
    got: list[bytes] = []
    with pytest.raises(FindFailedError):
        async for chunk in agen:
            got.append(chunk)
    assert len(got) == 2
    assert got[0].startswith(b"[") and got[1].startswith(b",")
    assert spy.puts == []
