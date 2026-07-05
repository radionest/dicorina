import pytest
import study_plan


def test_rejects_invalid_sizes():
    with pytest.raises(ValueError):
        study_plan.build_study_plan(0, 10)
    with pytest.raises(ValueError):
        study_plan.build_study_plan(3, 0)


def test_first_study_cyrillic_and_root_derived_uid():
    plan = study_plan.build_study_plan(6, 50)
    assert len(plan) == 6
    assert plan[0]["PatientName"] == study_plan.CYRILLIC_NAME == "Иванов^Пётр"
    assert plan[0]["StudyInstanceUID"] == study_plan.ROOT + ".1"
    assert any(ord(c) > 127 for c in plan[0]["PatientName"])


def test_uids_globally_unique_and_deterministic():
    plan = study_plan.build_study_plan(6, 50)
    sop = [u for s in plan for u in s["SOPInstanceUIDs"]]
    assert len(sop) == len(set(sop)) == 6 * 50
    assert study_plan.build_study_plan(6, 50) == plan


def test_bench_plan_counts_and_layout():
    bench = study_plan.build_bench_plan(big_instances=7, find_studies=15, find_instances=2)
    assert len(bench["big"]) == 2
    assert len(bench["multi"]) == 15
    assert all(len(s["SOPInstanceUIDs"]) == 7 for s in bench["big"])
    assert all(len(s["SOPInstanceUIDs"]) == 2 for s in bench["multi"])
    for s in bench["big"] + bench["multi"]:
        assert s["SeriesInstanceUID"] == s["StudyInstanceUID"] + ".1"
        assert set(s) == {"StudyInstanceUID", "SeriesInstanceUID", "PatientName",
                          "PatientID", "SOPInstanceUIDs"}


def test_bench_plan_patients():
    bench = study_plan.build_bench_plan()
    assert [s["PatientName"] for s in bench["big"]] == ["Bench^Big1", "Bench^Big2"]
    assert [s["PatientID"] for s in bench["big"]] == ["BENCH001", "BENCH002"]
    assert {s["PatientName"] for s in bench["multi"]} == {"Bench^Multi"}
    assert {s["PatientID"] for s in bench["multi"]} == {"BENCH100"}


def test_bench_plan_uids_disjoint_from_e2e_and_unique():
    bench = study_plan.build_bench_plan(big_instances=3, find_studies=15, find_instances=2)
    big_uids = [s["StudyInstanceUID"] for s in bench["big"]]
    multi_uids = [s["StudyInstanceUID"] for s in bench["multi"]]
    assert big_uids == [study_plan.ROOT + ".101", study_plan.ROOT + ".102"]
    assert multi_uids == [f"{study_plan.ROOT}.{200 + k}" for k in range(1, 16)]
    e2e_uids = {s["StudyInstanceUID"] for s in study_plan.build_study_plan(6, 1)}
    assert not set(big_uids + multi_uids) & e2e_uids
    sops = [u for s in bench["big"] + bench["multi"] for u in s["SOPInstanceUIDs"]]
    assert len(sops) == len(set(sops)) == 2 * 3 + 15 * 2
    assert study_plan.build_bench_plan(3, 15, 2) == study_plan.build_bench_plan(3, 15, 2)


def test_bench_plan_rejects_invalid_sizes():
    with pytest.raises(ValueError):
        study_plan.build_bench_plan(big_instances=0)
    with pytest.raises(ValueError):
        study_plan.build_bench_plan(find_studies=0)
    with pytest.raises(ValueError):
        study_plan.build_bench_plan(find_instances=0)
