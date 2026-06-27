"""Pure host-side assertions over collected vm-net JSON. No DICOM stack.
Each check_* returns a list of failure strings; empty == pass."""

import glob
import json
import os


def load_results(data_dir):
    out = {}
    for p in glob.glob(os.path.join(data_dir, "*.json")):
        with open(p, encoding="utf-8") as f:
            out[os.path.splitext(os.path.basename(p))[0]] = json.load(f)
    return out


def received_count(result, phase, study):
    return result.get("received", {}).get(phase, {}).get(study, {}).get("count", 0)


def _studies_in(result, phase):
    return set(result.get("received", {}).get(phase, {}).keys())


def event(result, kind):
    return next((e for e in result.get("events", []) if e.get("kind") == kind), None)


def _barrier_ok(result, phase):
    return any(e.get("kind") == "barrier" and e.get("phase") == phase and e.get("ok")
               for e in result.get("events", []))


def _drain_failed(result, phase):
    return any(e.get("kind") == "drain" and e.get("phase") == phase and not e.get("ok")
               for e in result.get("events", []))


def check_s0(clienta):
    fails = []
    # Hard: PACS rejects an unknown caller. Soft (verify-during-impl): proxy spoof — only a
    # hard reject if the DIMSE face enforces require_called_aet; otherwise it is observed.
    e = event(clienta, "direct_pacs_probe")
    if e is None or not e.get("rejected"):
        fails.append(f"S0: direct clienta->PACS probe not rejected ({e!r})")
    return fails


def check_s1(clientb, studies, cyrillic_study):
    fails = []
    e = event(clientb, "qido_list")
    if e is None or not e.get("ok"):
        fails.append(f"S1: QIDO study list failed ({e!r})")
    elif set(e.get("studies", [])) != set(studies):
        fails.append(f"S1: study list {set(e.get('studies', []))} != {set(studies)}")
    f = event(clientb, "qido_filtered")
    if f is None or set(f.get("studies", [])) != {cyrillic_study}:
        fails.append(f"S1: PatientName filter returned {f!r}, expected just {cyrillic_study}")
    return fails


def check_s2(clientb, study, n):  # noqa: ARG001
    fails = []
    e = event(clientb, "wado")
    if e is None or not e.get("ok"):
        fails.append(f"S2: WADO move-to-self failed ({e!r})")
    else:
        if e.get("metadata_count") != n:
            fails.append(f"S2: metadata count {e.get('metadata_count')} != {n}")
        if e.get("frame_bytes", 0) <= 0:
            fails.append("S2: empty frame body")
    return fails


def check_s3(clienta, study, n):
    fails = []
    if received_count(clienta, "s3", study) != n:
        fails.append(f"S3: clienta got {received_count(clienta, 's3', study)} of {n} for {study}")
    if _studies_in(clienta, "s3") != {study}:
        fails.append(f"S3: cross-contamination {_studies_in(clienta, 's3')}")
    g = event(clienta, "cmove_ghost")
    if g is None or not g.get("refused"):
        fails.append(f"S3: C-MOVE to unknown dest not refused ({g!r})")
    if _drain_failed(clienta, "s3"):
        fails.append("S3: drain timed out")
    return fails


def check_s4(clienta, clientb, cyrillic_name):
    fails = []
    cf = event(clienta, "cfind_cyrillic")
    if cf is None or cf.get("name") != cyrillic_name or not cf.get("ok"):
        fails.append(f"S4: DIMSE C-FIND cyrillic failed ({cf!r})")
    q = event(clientb, "qido_cyrillic")
    if q is None or q.get("name") != cyrillic_name or not q.get("ok"):
        fails.append(f"S4: HTTP QIDO cyrillic failed ({q!r})")
    return fails


def check_s5(clienta, clientb, study_a, study_b, n):
    fails = []
    if received_count(clienta, "s5", study_a) != n:
        fails.append(f"S5: clienta incomplete for {study_a}")
    if received_count(clientb, "s5", study_b) != n:
        fails.append(f"S5: clientb incomplete for {study_b}")
    if _studies_in(clienta, "s5") != {study_a}:
        fails.append(f"S5: clienta cross-contamination {_studies_in(clienta, 's5')}")
    if _studies_in(clientb, "s5") != {study_b}:
        fails.append(f"S5: clientb cross-contamination {_studies_in(clientb, 's5')}")
    if not (_barrier_ok(clienta, "s5") and _barrier_ok(clientb, "s5")):
        fails.append("S5: concurrency barrier did not synchronize")
    if _drain_failed(clienta, "s5") or _drain_failed(clientb, "s5"):
        fails.append("S5: a client's drain timed out")
    return fails  # distinct-AET-at-PACS is recorded as an observation, not asserted here


def check_s6(clienta, clientb, study, n):
    fails = []
    if received_count(clienta, "s6", study) != n:
        fails.append(
            f"S6: clienta DIMSE warm incomplete ({received_count(clienta, 's6', study)}/{n})"
        )
    e = event(clientb, "wado_cached")
    if e is None or not e.get("ok"):
        fails.append(f"S6: clientb HTTP read of warmed study failed ({e!r})")
    elif e.get("metadata_count") != n:
        fails.append(f"S6: clientb metadata count {e.get('metadata_count')} != {n}")
    return fails  # no-upstream-re-pull is recorded as an observation, not asserted here


def check_s7(proxy):
    # evicted_log_seen is recorded but observational per spec §8: INFO 'Evicted' is not
    # routed to journald by the deployed service (no root logger configured).
    fails = []
    before = proxy.get("studies_before_evict", 0)
    after = proxy.get("studies_after_evict", before)  # missing → after==before → fails
    if before < 1:
        fails.append(f"S7: expected >=1 cached study before eviction, got {before}")
    if not before > after:
        fails.append(f"S7: eviction did not reduce study count ({before} -> {after})")
    return fails
