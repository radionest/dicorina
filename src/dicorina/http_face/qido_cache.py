"""Short-TTL QIDO result cache (§6). Failures are never cached (caller-enforced)."""

from __future__ import annotations

from typing import Any

from cachetools import TTLCache


class QidoResultCache:
    def __init__(self, ttl_seconds: float = 5.0, max_entries: int = 256) -> None:
        self._enabled = ttl_seconds > 0
        self._cache: TTLCache[str, list[dict[str, Any]]] = TTLCache(
            maxsize=max_entries, ttl=ttl_seconds if self._enabled else 1.0
        )

    @staticmethod
    def key(scope: str, params: dict[str, str]) -> str:
        norm = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{scope}?{norm}"

    def get(self, key: str) -> list[dict[str, Any]] | None:
        return self._cache.get(key) if self._enabled else None

    def put(self, key: str, results: list[dict[str, Any]]) -> None:
        if self._enabled:
            self._cache[key] = results
