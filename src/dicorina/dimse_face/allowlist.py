"""Allowlist of known C-MOVE destinations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Destination:
    host: str
    port: int


class DestinationAllowlist:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._destinations: dict[str, Destination] = {}
        for aet, addr in mapping.items():
            host, sep, port_str = addr.rpartition(":")
            if not sep or not host or not port_str:
                raise ValueError(
                    f"Malformed allowlist entry for {aet!r}: {addr!r} (expected 'host:port')"
                )
            try:
                port = int(port_str)
            except ValueError as err:
                raise ValueError(
                    f"Invalid port in allowlist entry for {aet!r}: {addr!r}"
                ) from err
            self._destinations[aet] = Destination(host=host, port=port)

    def get(self, aet: str) -> Destination | None:
        return self._destinations.get(aet)

    def __contains__(self, aet: str) -> bool:
        return aet in self._destinations
