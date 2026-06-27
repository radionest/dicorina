import json
import tomllib
from pathlib import Path

CFG = Path(__file__).parent.parent / "staging" / "vm-net" / "config"
ALLOW_FLAGS = [
    "DicomAlwaysAllowEcho", "DicomAlwaysAllowStore", "DicomAlwaysAllowFind",
    "DicomAlwaysAllowMove", "DicomAlwaysAllowGet",
]


def test_pacs_knows_only_pool_aets_and_denies_defaults():
    pacs = json.loads((CFG / "pacs.json").read_text(encoding="utf-8"))
    assert pacs["DicomAet"] == "HOSPITALPACS"
    assert pacs["DicomCheckCalledAet"] is True
    assert pacs["DefaultEncoding"] == "Utf8"
    for flag in ALLOW_FLAGS:
        assert pacs[flag] is False, flag
    mods = pacs["DicomModalities"]
    assert {m[0] for m in mods.values()} == {"DICORINA1", "DICORINA2"}
    for m in mods.values():
        assert m[1] == "10.0.0.20" and m[2] == 11112  # move-to-self lands on proxy C-STORE SCP


def test_proxy_toml_pool_allowlist_and_pacs():
    proxy = tomllib.loads((CFG / "proxy.toml").read_text(encoding="utf-8"))
    assert proxy["pacs"]["host"] == "10.0.0.10" and proxy["pacs"]["aet"] == "HOSPITALPACS"
    assert proxy["pool"]["aets"] == ["DICORINA1", "DICORINA2"]
    assert proxy["scp"]["port"] == 11112
    assert proxy["dimse"]["listen_port"] == 4242
    assert proxy["dimse"]["allowlist"] == {"CLIENTA": "10.0.0.31:11112", "CLIENTB": "10.0.0.32:11112"}
    assert proxy["http"]["auth_token"] == ""
    assert proxy["healthcheck"]["test_study_uid"] == "1.2.826.0.1.3680043.8.498.1"
