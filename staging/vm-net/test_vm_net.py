"""Host-side e2e gate. Run AFTER run.sh collected the VM results:

    VMNET_DATA=staging/.data/vm-net uv run --with pytest pytest --noconftest \
        staging/vm-net/test_vm_net.py -v

Not in `testpaths`, so a bare `uv run pytest` never collects it."""

import os

import pytest
import study_plan
import vmnet_assert as va

DATA = os.environ.get("VMNET_DATA", "staging/.data/vm-net")
N = int(os.environ.get("INSTANCES_PER_STUDY", "50"))
PLAN = study_plan.build_study_plan(int(os.environ.get("STUDIES", "6")), N)
SUID = [s["StudyInstanceUID"] for s in PLAN]  # SUID[0]=study1 .. SUID[5]=study6


@pytest.fixture(scope="session")
def results():
    r = va.load_results(DATA)
    for need in ("clienta", "clientb", "proxy"):
        if need not in r:
            pytest.fail(f"missing {need}.json in {DATA} (collected: {sorted(r)})")
    return r


def test_s0_isolation(results):
    assert va.check_s0(results["clienta"]) == []


def test_s1_qido_live(results):
    assert va.check_s1(results["clientb"], SUID, SUID[0]) == []


def test_s2_wado_move_to_self(results):
    assert va.check_s2(results["clientb"], SUID[1], N) == []


def test_s3_dimse_passthrough(results):
    assert va.check_s3(results["clienta"], SUID[2], N) == []


def test_s4_cyrillic_both_faces(results):
    assert va.check_s4(results["clienta"], results["clientb"], study_plan.CYRILLIC_NAME) == []


def test_s5_aet_pool_concurrency(results):
    assert va.check_s5(results["clienta"], results["clientb"], SUID[3], SUID[4], N) == []
    pacs = results.get("pacs", {})
    print("OBSERVED PACS move requests:", pacs.get("move_requests"),
          "distinct callers:", pacs.get("distinct_callers"))


def test_s6_cross_face_cache_hit(results):
    assert va.check_s6(results["clienta"], results["clientb"], SUID[5], N) == []
    print("OBSERVED PACS move requests (no-repull is observational):",
          results.get("pacs", {}).get("move_requests"))


def test_s7_eviction(results):
    assert va.check_s7(results["proxy"]) == []
    print("OBSERVED S7 evicted_log_seen:", results["proxy"].get("evicted_log_seen"))
