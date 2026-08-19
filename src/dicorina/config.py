"""Configuration model + TOML loader (§10)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PacsConfig(BaseModel):
    host: str
    port: int = 104
    aet: str = "PACS"
    store_aet: str = ""


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
    model_config = ConfigDict(extra="forbid")

    dir: Path
    memory_ttl_minutes: int = 30
    memory_max_size_gb: float = 4.0
    disk_ttl_hours: int = 24
    disk_max_size_gb: float = 10.0
    qido_ttl_seconds: float = 5.0
    eviction_interval_seconds: float = 300.0


class TimeoutsConfig(BaseModel):
    cfind: float = 30.0
    cmove: float = 300.0
    arrival: float = 60.0
    completion_grace: float = 5.0
    find_lease: float = 30.0
    store: float = 30.0


class HealthcheckConfig(BaseModel):
    interval_seconds: float = 300.0
    test_study_uid: str = ""
    test_series_uid: str = ""


_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # dicorina's own loggers plus the dimsechord core.
    level: str = "INFO"
    # pynetdicom is a separate knob, and an expensive one: its default
    # LOG_HANDLER_LEVEL="standard" binds per-DIMSE-message handlers, so INFO
    # costs about three lines per relayed instance (thousands per study),
    # written synchronously to the journal by every association thread. Raising
    # it is NOT needed to see refused associations — the face logs those at
    # WARNING with the decoded reason. Use INFO for a short capture on a quiet
    # box; DEBUG dumps every PDU on top of that.
    pynetdicom_level: str = "WARNING"
    # Interval of the periodic load snapshot (0 disables it).
    snapshot_interval_seconds: float = 60.0
    # A DIMSE handler that runs at least this long logs at WARNING when it
    # finishes, and an operation in flight for longer marks the load snapshot
    # as stuck: it has held an association slot for that whole time. Must be
    # > 0 — unlike snapshot_interval_seconds, 0 would not disable the warning
    # but fire it on every single operation.
    slow_operation_seconds: float = Field(default=10.0, gt=0)

    @field_validator("level", "pynetdicom_level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> str:
        return str(value).upper()

    @field_validator("level", "pynetdicom_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        if value not in _LOG_LEVELS:
            raise ValueError(f"unknown log level {value!r}; expected one of {_LOG_LEVELS}")
        return value


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
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ohif: OhifConfig = Field(default_factory=OhifConfig)


def load_config(path: str | Path) -> DicorinaConfig:
    """Load + validate the TOML config; DICORINA_* env vars override matching keys.

    The log-level overrides exist so an operator can raise verbosity on a
    running box with a drop-in unit override and a restart, without editing
    the deployed config file.
    """
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    token = os.environ.get("DICORINA_AUTH_TOKEN")
    if token is not None:
        data.setdefault("http", {})["auth_token"] = token
    for env_var, key in (
        ("DICORINA_LOG_LEVEL", "level"),
        ("DICORINA_PYNETDICOM_LOG_LEVEL", "pynetdicom_level"),
    ):
        level = os.environ.get(env_var)
        if level is not None:
            data.setdefault("logging", {})[key] = level
    return DicorinaConfig.model_validate(data)
