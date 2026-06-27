from __future__ import annotations

import asyncio

import pytest
from dimsechord import DicomCache

from dicorina.eviction import EvictionLoop
from tests.factories import make_instance


@pytest.mark.asyncio
async def test_run_once_evicts_expired(tmp_path) -> None:
    cache = DicomCache(tmp_path / "c", ttl_hours=0, max_size_gb=10.0)
    ds = make_instance("1.2", "1.2.1", "1.2.1.1")
    cache.write_instance("1.2", "1.2.1", "1.2.1.1", ds)
    assert cache.series_cached("1.2", "1.2.1")

    loop = EvictionLoop(cache, interval_seconds=9999.0)
    expired, by_size = await loop.run_once()
    assert expired >= 1
    assert by_size >= 0  # by_size may or may not evict anything
    assert not cache.series_cached("1.2", "1.2.1")
    cache.shutdown()


@pytest.mark.asyncio
async def test_start_stop_is_clean(tmp_path) -> None:
    cache = DicomCache(tmp_path / "c")
    loop = EvictionLoop(cache, interval_seconds=0.05)
    loop.start()
    await asyncio.sleep(0.15)
    loop.stop()
    await asyncio.sleep(0.05)
    cache.shutdown()
