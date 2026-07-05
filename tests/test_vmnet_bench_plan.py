import bench_plan


def test_cold_round_split_swaps_and_covers_all():
    r0 = bench_plan.cold_round_split(0)
    r1 = bench_plan.cold_round_split(1)
    assert r0["cmove_cold"] == [0, 1, 2]
    assert r0["wado_frame_cold"] == [3, 4, 5]
    assert r1["cmove_cold"] == [3, 4, 5]
    assert r1["wado_frame_cold"] == [0, 1, 2]
    assert sorted(r0["cmove_cold"] + r0["wado_frame_cold"]) == list(range(6))
    assert bench_plan.cold_round_split(2) == r0


def test_qido_query_deterministic_unique_and_url_safe():
    qs = [bench_plan.qido_query(i, warm=w) for i in range(100) for w in (False, True)]
    assert len(qs) == len(set(qs))
    assert bench_plan.qido_query(0) == "PatientName=Patient%5E2&limit=101"
    assert bench_plan.qido_query(0, warm=True) == "PatientName=Patient%5E2&limit=201"
    assert bench_plan.qido_query(5) == "PatientName=Patient%5E2&limit=106"
    assert "^" not in "".join(qs)  # raw '^' is invalid in a URL query


def test_cold_round_split_two_big_studies():
    r0 = bench_plan.cold_round_split(0, 2)
    r1 = bench_plan.cold_round_split(1, 2)
    assert r0 == {"cmove_cold": [0], "wado_frame_cold": [1]}
    assert r1 == {"cmove_cold": [1], "wado_frame_cold": [0]}
    assert bench_plan.cold_round_split(2, 2) == r0
