import pytest

from dicorina.dimse_face.allowlist import DestinationAllowlist


def test_resolve_known() -> None:
    al = DestinationAllowlist({"WS": "10.0.0.31:11112"})
    dest = al.resolve("WS")
    assert dest is not None
    assert dest.host == "10.0.0.31"
    assert dest.port == 11112


def test_resolve_unknown_is_none() -> None:
    assert DestinationAllowlist({}).resolve("NOPE") is None


def test_invalid_entry_rejected() -> None:
    with pytest.raises(ValueError):
        DestinationAllowlist({"WS": "no-port-here"})
