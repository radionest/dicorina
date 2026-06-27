"""Periodic cache eviction loop (§6): TTL + size cap, off the request path."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dimsechord import DicomCache

logger = logging.getLogger(__name__)


class EvictionLoop:
    def __init__(self, cache: DicomCache, interval_seconds: float = 300.0) -> None:
        self._cache = cache
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> tuple[int, int]:
        expired = await asyncio.to_thread(self._cache.evict_expired)
        by_size = await asyncio.to_thread(self._cache.evict_by_size)
        return expired, by_size

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self.run_once()
                except Exception as e:  # never let the loop die
                    logger.error(f"Eviction pass failed: {e}")
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
