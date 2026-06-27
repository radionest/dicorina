from pathlib import Path

import pytest

from dicorina.config import DicorinaConfig, load_config

_MINIMAL = """
[pacs]
host = "10.0.0.10"
port = 104
aet = "HOSPITALPACS"

[scp]
port = 11112

[cache]
dir = "/var/cache/dicorina"
"""


def test_load_minimal_applies_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "dicorina.toml"
    cfg_file.write_text(_MINIMAL, encoding="utf-8")
    cfg = load_config(cfg_file)
    assert isinstance(cfg, DicorinaConfig)
    assert cfg.pacs.aet == "HOSPITALPACS"
    assert cfg.pool.aets == ["DICORINA"]
    assert cfg.pool.per_aet_cap == 1
    assert cfg.http.bind_host == "127.0.0.1"
    assert cfg.http.auth_token == ""
    assert cfg.cache.qido_ttl_seconds == 5.0
    assert cfg.timeouts.cmove == 300.0


def test_allowlist_and_pool_parse(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.toml"
    cfg_file.write_text(
        _MINIMAL
        + '\n[pool]\naets = ["DICORINA1", "DICORINA2"]\nper_aet_cap = 2\n'
        + '\n[dimse.allowlist]\nWORKSTATION = "10.0.0.31:11112"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.pool.aets == ["DICORINA1", "DICORINA2"]
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
    cfg_file.write_text(_MINIMAL + "\n[pool]\naets = []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_file)
