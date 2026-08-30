from pathlib import Path

import pytest

from dicorina.config import DicorinaConfig, load_config

_MINIMAL = """
[pacs]
host = "10.0.0.10"
port = 104
aet = "HOSPITALPACS"

[scp]

[cache]
dir = "/var/cache/dicorina"
"""


def test_load_minimal_applies_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "dicorina.toml"
    cfg_file.write_text(_MINIMAL, encoding="utf-8")
    cfg = load_config(cfg_file)
    assert isinstance(cfg, DicorinaConfig)
    assert cfg.pacs.aet == "HOSPITALPACS"
    assert [m.aet for m in cfg.pool.members] == ["DICORINA"]
    assert cfg.pool.members[0].port == 11112
    assert cfg.pool.per_aet_cap == 1
    assert cfg.http.bind_host == "127.0.0.1"
    assert cfg.http.auth_token == ""
    assert cfg.cache.qido_ttl_seconds == 5.0
    assert cfg.cache.memory_max_size_gb == 4.0
    assert cfg.timeouts.move_lease == 5.0
    assert cfg.timeouts.find_lease == 30.0
    assert cfg.scp.max_associations == 25
    assert cfg.scp.session_queue_maxsize == 64


def test_allowlist_and_pool_parse(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        _MINIMAL
        + "\n[pool]\nper_aet_cap = 2\n"
        + '[[pool.members]]\naet = "DICORINA1"\nport = 11112\n'
        + '[[pool.members]]\naet = "DICORINA2"\nport = 11113\n'
        + '\n[dimse.allowlist]\nWORKSTATION = "10.0.0.31:11112"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert [m.aet for m in cfg.pool.members] == ["DICORINA1", "DICORINA2"]
    assert [m.port for m in cfg.pool.members] == [11112, 11113]
    assert cfg.pool.per_aet_cap == 2
    assert cfg.dimse.allowlist["WORKSTATION"] == "10.0.0.31:11112"


def test_auth_token_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL, encoding="utf-8")
    monkeypatch.setenv("DICORINA_AUTH_TOKEN", "s3cret")
    cfg = load_config(cfg_file)
    assert cfg.http.auth_token == "s3cret"


def test_empty_pool_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + "\n[pool]\nmembers = []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_duplicate_aet_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        _MINIMAL
        + '\n[[pool.members]]\naet = "DUP"\nport = 11112\n'
        + '[[pool.members]]\naet = "DUP"\nport = 11113\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_duplicate_port_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        _MINIMAL
        + '\n[[pool.members]]\naet = "AAA"\nport = 11112\n'
        + '[[pool.members]]\naet = "BBB"\nport = 11112\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_legacy_pool_aets_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + '\n[pool]\naets = ["DICORINA"]\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_legacy_scp_port_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        '[pacs]\nhost = "10.0.0.10"\naet = "HOSPITALPACS"\n'
        "[scp]\nport = 104\n"
        '[cache]\ndir = "/var/cache/dicorina"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_legacy_memory_max_entries_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + "memory_max_entries = 50\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_example_config_is_valid() -> None:
    example = Path(__file__).parents[2] / "deploy" / "config.example.toml"
    cfg = load_config(example)
    assert [m.aet for m in cfg.pool.members] == ["DICORINA"]
    assert cfg.pool.members[0].port == 11112


def test_dimse_aet_and_find_cap_defaults(tmp_path) -> None:
    cfg = DicorinaConfig.model_validate(
        {"pacs": {"host": "h"}, "scp": {}, "cache": {"dir": str(tmp_path)}}
    )
    assert cfg.dimse.aet == "DICORINA"
    assert cfg.pool.per_aet_find_cap == 4


def test_dimse_aet_and_find_cap_from_toml(tmp_path) -> None:
    cfg = DicorinaConfig.model_validate(
        {
            "pacs": {"host": "h"},
            "scp": {},
            "cache": {"dir": str(tmp_path)},
            "dimse": {"aet": "FACE1"},
            "pool": {"members": [{"aet": "P1", "port": 1}], "per_aet_find_cap": 2},
            "timeouts": {"find_lease": 0.5},
        }
    )
    assert cfg.dimse.aet == "FACE1"
    assert cfg.pool.per_aet_find_cap == 2
    assert cfg.timeouts.find_lease == 0.5


def test_logging_defaults(tmp_path) -> None:
    cfg = DicorinaConfig.model_validate(
        {"pacs": {"host": "h"}, "scp": {}, "cache": {"dir": str(tmp_path)}}
    )
    assert cfg.logging.level == "INFO"
    assert cfg.logging.pynetdicom_level == "WARNING"
    assert cfg.logging.snapshot_interval_seconds == 60.0
    assert cfg.logging.slow_operation_seconds == 10.0


def test_zero_slow_operation_seconds_rejected(tmp_path) -> None:
    """0 disables snapshot_interval_seconds, but would make every operation
    warn here — reject it rather than invert the neighbouring knob's meaning."""
    with pytest.raises(ValueError):
        DicorinaConfig.model_validate(
            {
                "pacs": {"host": "h"},
                "scp": {},
                "cache": {"dir": str(tmp_path)},
                "logging": {"slow_operation_seconds": 0},
            }
        )


def test_log_level_is_case_insensitive(tmp_path) -> None:
    cfg = DicorinaConfig.model_validate(
        {
            "pacs": {"host": "h"},
            "scp": {},
            "cache": {"dir": str(tmp_path)},
            "logging": {"level": "debug"},
        }
    )
    assert cfg.logging.level == "DEBUG"


def test_unknown_log_level_rejected(tmp_path) -> None:
    """A typo must fail at startup, not silently leave the journal empty."""
    with pytest.raises(ValueError):
        DicorinaConfig.model_validate(
            {
                "pacs": {"host": "h"},
                "scp": {},
                "cache": {"dir": str(tmp_path)},
                "logging": {"level": "VERBOSE"},
            }
        )


def test_log_level_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising verbosity on a running box must not require editing the config."""
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL, encoding="utf-8")
    monkeypatch.setenv("DICORINA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DICORINA_PYNETDICOM_LOG_LEVEL", "INFO")
    cfg = load_config(cfg_file)
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.pynetdicom_level == "INFO"


def test_store_config_defaults(tmp_path) -> None:
    cfg = DicorinaConfig.model_validate(
        {"pacs": {"host": "10.0.0.1"}, "scp": {}, "cache": {"dir": str(tmp_path)}}
    )
    assert cfg.timeouts.store == 30.0
    assert cfg.pacs.store_aet == ""


def test_store_config_overrides(tmp_path) -> None:
    cfg = DicorinaConfig.model_validate(
        {
            "pacs": {"host": "10.0.0.1", "store_aet": "DICSTORE"},
            "scp": {},
            "cache": {"dir": str(tmp_path)},
            "timeouts": {"store": 5.0},
        }
    )
    assert cfg.timeouts.store == 5.0
    assert cfg.pacs.store_aet == "DICSTORE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cfind", 6666660.0),  # the production value that removed every bound on a stuck query
        ("move_lease", 61.0),
        ("arrival", 601.0),
        ("completion_grace", 61.0),
        ("find_lease", 301.0),
        ("store", 301.0),
    ],
)
def test_timeout_above_bound_rejected(tmp_path: Path, field: str, value: float) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + f"\n[timeouts]\n{field} = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        load_config(cfg_file)


@pytest.mark.parametrize(
    "field",
    ["cfind", "arrival", "completion_grace", "find_lease", "store", "move_lease"],
)
@pytest.mark.parametrize("value", [0.0, -1.0])
def test_non_positive_timeout_rejected(tmp_path: Path, field: str, value: float) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + f"\n[timeouts]\n{field} = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        load_config(cfg_file)


def test_timeouts_at_their_upper_bound_load(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        _MINIMAL
        + "\n[timeouts]\ncfind = 300.0\narrival = 600.0\n"
        + "completion_grace = 60.0\nfind_lease = 300.0\nstore = 300.0\nmove_lease = 60.0\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.timeouts.cfind == 300.0
    assert cfg.timeouts.arrival == 600.0
    assert cfg.timeouts.completion_grace == 60.0
    assert cfg.timeouts.find_lease == 300.0
    assert cfg.timeouts.store == 300.0
    assert cfg.timeouts.move_lease == 60.0


@pytest.mark.parametrize("field", ["per_aet_cap", "per_aet_find_cap"])
def test_zero_pool_cap_rejected(tmp_path: Path, field: str) -> None:
    """0 is not "unlimited" — it is a pool in which every lease times out."""
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + f"\n[pool]\n{field} = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        load_config(cfg_file)


def test_large_pool_caps_accepted(tmp_path: Path) -> None:
    """No upper bound: the real ceiling is the upstream PACS's per-AET
    tolerance, which dicorina does not know (design D2)."""
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + "\n[pool]\nper_aet_find_cap = 512\n", encoding="utf-8")
    assert load_config(cfg_file).pool.per_aet_find_cap == 512


def test_stale_cmove_key_rejected(tmp_path: Path) -> None:
    """dimsechord 0.8.0 made cmove_timeout inert, so the key is gone. A config
    still carrying it must fail rather than let the operator believe it bounds
    anything (design D2)."""
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL + "\n[timeouts]\ncmove = 500.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cmove"):
        load_config(cfg_file)


def test_scp_caps_parse(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        _MINIMAL.replace("[scp]\n", "[scp]\nmax_associations = 8\nsession_queue_maxsize = 16\n"),
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.scp.max_associations == 8
    assert cfg.scp.session_queue_maxsize == 16


@pytest.mark.parametrize("field", ["max_associations", "session_queue_maxsize"])
def test_zero_scp_cap_rejected(tmp_path: Path, field: str) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(_MINIMAL.replace("[scp]\n", f"[scp]\n{field} = 0\n"), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        load_config(cfg_file)
