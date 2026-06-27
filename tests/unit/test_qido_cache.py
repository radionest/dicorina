import time

from dicorina.http_face.qido_cache import QidoResultCache


def test_key_is_order_independent() -> None:
    c = QidoResultCache(ttl_seconds=5.0)
    assert c.key("STUDY", {"a": "1", "b": "2"}) == c.key("STUDY", {"b": "2", "a": "1"})


def test_put_get_roundtrip() -> None:
    c = QidoResultCache(ttl_seconds=5.0)
    k = c.key("STUDY", {"PatientID": "P1"})
    c.put(k, [{"x": 1}])
    assert c.get(k) == [{"x": 1}]


def test_disabled_when_ttl_zero() -> None:
    c = QidoResultCache(ttl_seconds=0.0)
    k = c.key("STUDY", {})
    c.put(k, [{"x": 1}])
    assert c.get(k) is None


def test_expires_after_ttl() -> None:
    c = QidoResultCache(ttl_seconds=0.05)
    k = c.key("STUDY", {})
    c.put(k, [{"x": 1}])
    time.sleep(0.1)
    assert c.get(k) is None
