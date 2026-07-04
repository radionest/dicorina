"""Configuration model + TOML loader (§10)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PacsConfig(BaseModel):
    host: str
    port: int = 104
    aet: str = "PACS"


class AetPoolMember(BaseModel):
    aet: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)


class PoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[AetPoolMember] = Field(
        default_factory=lambda: [AetPoolMember(aet="DICORINA", port=11112)],
        min_length=1,
    )
    per_aet_cap: int = 1
    per_aet_find_cap: int = 4

    @model_validator(mode="after")
    def _unique_aets_and_ports(self) -> PoolConfig:
        aets = [m.aet for m in self.members]
        if len(set(aets)) != len(aets):
            raise ValueError("pool.members has duplicate AETs")
        ports = [m.port for m in self.members]
        if len(set(ports)) != len(ports):
            raise ValueError("pool.members has duplicate ports")
        return self


class ScpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind_ip: str = "0.0.0.0"


class DimseConfig(BaseModel):
    aet: str = "DICORINA"
    listen_ip: str = "0.0.0.0"
    listen_port: int = 4242
    allowlist: dict[str, str] = Field(default_factory=dict)


class HttpConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    auth_token: str = ""


class CacheConfig(BaseModel):
    dir: Path
    memory_ttl_minutes: int = 30
    memory_max_entries: int = 50
    disk_ttl_hours: int = 24
    disk_max_size_gb: float = 10.0
    qido_ttl_seconds: float = 5.0
    eviction_interval_seconds: float = 300.0


class TimeoutsConfig(BaseModel):
    cfind: float = 30.0
    cmove: float = 300.0
    arrival: float = 60.0
    completion_grace: float = 5.0


class HealthcheckConfig(BaseModel):
    interval_seconds: float = 300.0
    test_study_uid: str = ""
    test_series_uid: str = ""


class OhifConfig(BaseModel):
    enabled: bool = False
    friendly_name: str = "dicorina"
    external_root: str | None = None


class DicorinaConfig(BaseModel):
    pacs: PacsConfig
    scp: ScpConfig
    cache: CacheConfig
    pool: PoolConfig = Field(default_factory=PoolConfig)
    dimse: DimseConfig = Field(default_factory=DimseConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    ohif: OhifConfig = Field(default_factory=OhifConfig)


def load_config(path: str | Path) -> DicorinaConfig:
    """Load + validate the TOML config; DICORINA_AUTH_TOKEN env overrides http.auth_token."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    token = os.environ.get("DICORINA_AUTH_TOKEN")
    if token is not None:
        data.setdefault("http", {})["auth_token"] = token
    return DicorinaConfig.model_validate(data)
