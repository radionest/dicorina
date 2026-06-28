import vmnet_assert as va

S1, S2, S3, S4, S5, S6 = (f"1.2.826.0.1.3680043.8.498.{i}" for i in range(1, 7))
N = 50


def _client(role, received=None, events=None):
    return {"role": role, "aet": role.upper(), "received": received or {}, "events": events or []}


def test_s0_pass_and_fail():
    ok = _client("clienta", events=[
        {"kind": "direct_pacs_probe", "rejected": True},
        {"kind": "spoof_proxy_probe", "rejected": True}])
    assert va.check_s0(ok) == []
    bad = _client("clienta", events=[{"kind": "direct_pacs_probe", "rejected": False}])
    assert va.check_s0(bad)


def test_s1_list_and_filter():
    cb = _client("clientb", events=[
        {"kind": "qido_list", "studies": [S1, S2, S3, S4, S5, S6], "ok": True},
        {"kind": "qido_filtered", "studies": [S1], "ok": True}])
    assert va.check_s1(cb, [S1, S2, S3, S4, S5, S6], S1) == []
    cb_bad = _client("clientb", events=[
        {"kind": "qido_list", "studies": [S1, S2], "ok": True},
        {"kind": "qido_filtered", "studies": [S1], "ok": True}])
    assert va.check_s1(cb_bad, [S1, S2, S3, S4, S5, S6], S1)


def test_s2_wado_metadata_and_frame():
    cb = _client("clientb", events=[
        {"kind": "wado", "study": S2, "metadata_count": N, "frame_bytes": 512, "ok": True}])
    assert va.check_s2(cb, S2, N) == []
    cb_bad = _client("clientb", events=[
        {"kind": "wado", "study": S2, "metadata_count": N, "frame_bytes": 0, "ok": True}])
    assert va.check_s2(cb_bad, S2, N)


def test_s3_passthrough_and_ghost():
    ca = _client("clienta", {"s3": {S3: {"count": N, "from": "DICORINA1"}}},
                 [{"kind": "cmove_ghost", "refused": True}])
    assert va.check_s3(ca, S3, N) == []
    ca_bad = _client("clienta", {"s3": {S3: {"count": N, "from": "DICORINA1"}}},
                     [{"kind": "cmove_ghost", "refused": False}])
    assert va.check_s3(ca_bad, S3, N)


def test_s4_cyrillic_both_faces():
    ca = _client("clienta", events=[{"kind": "cfind_cyrillic", "name": "Иванов^Пётр", "ok": True}])
    cb = _client("clientb", events=[{"kind": "qido_cyrillic", "name": "Иванов^Пётр", "ok": True}])
    assert va.check_s4(ca, cb, "Иванов^Пётр") == []
    cb_bad = _client("clientb", events=[
        {"kind": "qido_cyrillic", "name": "Ivanov^Petr", "ok": True}])
    assert va.check_s4(ca, cb_bad, "Иванов^Пётр")


def test_s5_concurrent_isolation():
    bar = {"kind": "barrier", "phase": "s5", "ok": True}
    ca = _client("clienta", {"s5": {S4: {"count": N, "from": "DICORINA1"}}}, [bar])
    cb = _client("clientb", {"s5": {S5: {"count": N, "from": "DICORINA2"}}}, [bar])
    assert va.check_s5(ca, cb, S4, S5, N) == []
    ca_x = _client("clienta", {"s5": {
        S4: {"count": N, "from": "x"}, S5: {"count": 3, "from": "x"}}}, [bar])
    assert va.check_s5(ca_x, cb, S4, S5, N)  # cross-contamination


def test_s6_cross_face():
    ca = _client("clienta", {"s6": {S6: {"count": N, "from": "DICORINA1"}}})
    cb = _client("clientb", events=[
        {"kind": "wado_cached", "study": S6, "metadata_count": N, "ok": True}])
    assert va.check_s6(ca, cb, S6, N) == []
    cb_bad = _client("clientb", events=[
        {"kind": "wado_cached", "study": S6, "metadata_count": 0, "ok": False}])
    assert va.check_s6(ca, cb_bad, S6, N)


def test_s7_eviction():
    # pass: eviction happened, log seen
    assert va.check_s7({
        "studies_before_evict": 4, "studies_after_evict": 0, "evicted_log_seen": True}) == []
    # pass: eviction happened, log NOT seen — observational only per spec §8
    assert va.check_s7({
        "studies_before_evict": 4, "studies_after_evict": 0, "evicted_log_seen": False}) == []
    # fail: no reduction in study count
    assert va.check_s7({
        "studies_before_evict": 4, "studies_after_evict": 4, "evicted_log_seen": True})
    # fail: missing after key → fail-closed (after defaults to before → no reduction)
    assert va.check_s7({"studies_before_evict": 4})
    # fail: nothing was cached before eviction
    assert va.check_s7({"studies_before_evict": 0, "studies_after_evict": 0})


def test_load_results_tolerates_bad_sidecar(tmp_path):
    (tmp_path / "clienta.json").write_text(
        '{"role":"clienta","aet":"CLIENTA","received":{},"events":[]}', encoding="utf-8"
    )
    (tmp_path / "proxy-health.json").write_bytes(b"")
    results = va.load_results(str(tmp_path))
    assert "clienta" in results
    assert "proxy-health" not in results
