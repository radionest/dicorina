"""Short-TTL QIDO body cache (§6). Failures are never cached (caller-enforced)."""

from __future__ import annotations

from cachetools import TTLCache


class QidoResultCache:
    def __init__(self, ttl_seconds: float = 5.0, max_entries: int = 256) -> None:
        self._enabled = ttl_seconds > 0
        self._cache: TTLCache[str, bytes] = TTLCache(
            maxsize=max_entries, ttl=ttl_seconds if self._enabled else 1.0
        )

    @staticmethod
    def key(scope: str, params: dict[str, str], includefields: list[str] | None = None) -> str:
        norm = "&".join(f"{k}={params[k]}" for k in sorted(params))
        inc = ",".join(sorted(includefields or []))
        return f"{scope}?{norm}&includefield={inc}"

    def get(self, key: str) -> bytes | None:
        return self._cache.get(key) if self._enabled else None

    def put(self, key: str, body: bytes) -> None:
        if self._enabled:
            self._cache[key] = body
