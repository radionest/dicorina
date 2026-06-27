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
