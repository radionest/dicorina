"""Latency bench agent: times identical operations against the PACS directly and
through dicorina, from the same VM (one clock — no cross-VM sync). Interleaves
direct/proxy reps so host-load drift hits both paths equally. Writes raw samples;
the host computes stats. Python 3.7 (client golden VM)."""

import contextlib
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

from pydicom.dataset import Dataset
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bench_plan
import study_plan

SELF_AET = os.environ["SELF_AET"]
SCP_PORT = int(os.environ["SCP_PORT"])
DATA_DIR = os.environ["DATA_DIR"]
RESULT_PATH = os.environ["RESULT_PATH"]
REPS = int(os.environ.get("BENCH_REPS", "20"))
MOVE_REPS = int(os.environ.get("BENCH_MOVE_REPS", "10"))
COLD_ROUNDS = int(os.environ.get("BENCH_COLD_ROUNDS", "2"))
BIG_INSTANCES = int(os.environ.get("BENCH_BIG_INSTANCES", "1000"))
FIND_STUDIES = int(os.environ.get("BENCH_FIND_STUDIES", "15"))
FIND_INSTANCES = int(os.environ.get("BENCH_FIND_INSTANCES", "2"))
WARMUP = 3

BENCH = study_plan.build_bench_plan(BIG_INSTANCES, FIND_STUDIES, FIND_INSTANCES)
BIG = BENCH["big"]  # the 2 big studies: move/WADO scenarios
MULTI = BENCH["multi"][0]  # PatientName source for the 15-study C-FIND

DIRECT = {
    "host": os.environ["PACS_HOST"], "port": int(os.environ["PACS_DICOM"]),
    "called": os.environ["PACS_AET"],
    "http": f"http://{os.environ['PACS_HOST']}:{os.environ['PACS_HTTP']}",
}
PROXY = {
    "host": os.environ["PROXY_HOST"], "port": int(os.environ["PROXY_DIMSE"]),
    "called": os.environ["PROXY_CALLED_AET"],
    "http": f"http://{os.environ['PROXY_HOST']}:{os.environ['PROXY_HTTP']}",
}
TARGET = {"direct": DIRECT, "proxy": PROXY}

samples = []
_store = {"n": 0}
_lock = threading.Lock()


def _on_store(event):  # noqa: ARG001 - pynetdicom handler signature
    with _lock:
        _store["n"] += 1
    return 0x0000


def reset_stores():
    with _lock:
        _store["n"] = 0


def stores():
    with _lock:
        return _store["n"]


def start_scp():
    ae = AE(ae_title=SELF_AET)
    ae.supported_contexts = StoragePresentationContexts
    return ae.start_server(
        ("0.0.0.0", SCP_PORT), block=False, evt_handlers=[(evt.EVT_C_STORE, _on_store)]
    )


def record(scenario, path, rep, t_ms, ok, error=None, study=None):
    samples.append({"scenario": scenario, "path": path, "rep": rep, "study": study,
                    "t_ms": t_ms, "ok": bool(ok), "error": error})


# --- timed primitives (full client-visible operation, association included) ---

def timed_cecho(target):
    ae = AE(ae_title=SELF_AET)
    ae.add_requested_context(Verification)
    t0 = time.perf_counter()
    assoc = ae.associate(target["host"], target["port"], ae_title=target["called"])
    ok, err = False, "association rejected"
    if assoc.is_established:
        status = assoc.send_c_echo()
        ok = bool(status) and int(status.Status) == 0x0000
        err = None if ok else f"echo status={getattr(status, 'Status', None)}"
        assoc.release()
    return (time.perf_counter() - t0) * 1000.0, ok, err


def timed_cfind(target, query, expect_n=None):
    ae = AE(ae_title=SELF_AET)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
    n, err = 0, None
    t0 = time.perf_counter()
    assoc = ae.associate(target["host"], target["port"], ae_title=target["called"])
    if not assoc.is_established:
        return (time.perf_counter() - t0) * 1000.0, False, "association rejected"
    try:
        for _st, ident in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind):
            if ident is not None:
                n += 1
        assoc.release()
    except Exception as exc:
        err = repr(exc)
        assoc.abort()
    t_ms = (time.perf_counter() - t0) * 1000.0
    if err is None:
        if expect_n is not None and n != expect_n:
            err = f"{n} results, expected {expect_n}"
        elif expect_n is None and n == 0:
            err = "0 results"
    return t_ms, err is None, err


def timed_cmove(target, study_uid, expect_n):
    ae = AE(ae_title=SELF_AET)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
    reset_stores()
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = study_uid
    final, err = None, None
    t0 = time.perf_counter()
    assoc = ae.associate(target["host"], target["port"], ae_title=target["called"])
    if not assoc.is_established:
        return (time.perf_counter() - t0) * 1000.0, False, "association rejected"
    try:
        for status, _ in assoc.send_c_move(
            query, SELF_AET, StudyRootQueryRetrieveInformationModelMove
        ):
            if status:
                final = int(status.Status)
        assoc.release()
    except Exception as exc:
        err = repr(exc)
        assoc.abort()
    t_ms = (time.perf_counter() - t0) * 1000.0
    got = stores()
    ok = err is None and final == 0x0000 and got == expect_n
    if not ok and err is None:
        err = f"final={final} stores={got}/{expect_n}"
    return t_ms, ok, err


def timed_http(url, timeout=120):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
        ok = len(body) > 0
        err = None if ok else "empty body"
    except Exception as exc:
        ok, err = False, repr(exc)
    return (time.perf_counter() - t0) * 1000.0, ok, err


# --- query/url builders ---

def q_patient_studies(name):
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = ""
    query.PatientName = name
    return query


def q_series(study_uid):
    query = Dataset()
    query.QueryRetrieveLevel = "SERIES"
    query.StudyInstanceUID = study_uid
    query.SeriesInstanceUID = ""
    return query


def meta_url(base, study):
    return f"{base}/dicom-web/studies/{study['StudyInstanceUID']}/metadata"


def frame_url(base, study):
    return (f"{base}/dicom-web/studies/{study['StudyInstanceUID']}"
            f"/series/{study['SeriesInstanceUID']}"
            f"/instances/{study['SOPInstanceUIDs'][0]}/frames/1")


def qido_url(base, query_string):
    return f"{base}/dicom-web/studies?{query_string}"


# --- scenario runners ---

def run_interleaved(scenario, fn, reps, warmup=WARMUP):
    """fn(path, rep) -> (t_ms, ok, err, study). Warm-up reps hit both paths, discarded.
    Warm-up rep indices are negative so fn can still vary its inputs safely."""
    for w in range(warmup):
        fn("direct", -1 - w)
        fn("proxy", -1 - w)
    for i in range(reps):
        for path in ("direct", "proxy"):
            t_ms, ok, err, study = fn(path, i)
            record(scenario, path, i, t_ms, ok, err, study)


def bench_cfind_study():
    def fn(path, _rep):
        t_ms, ok, err = timed_cfind(
            TARGET[path], q_patient_studies(MULTI["PatientName"]), expect_n=FIND_STUDIES
        )
        return t_ms, ok, err, None
    run_interleaved("cfind_study", fn, REPS)


def bench_cfind_series():
    uid = BIG[0]["StudyInstanceUID"]

    def fn(path, _rep):
        t_ms, ok, err = timed_cfind(TARGET[path], q_series(uid))
        return t_ms, ok, err, uid
    run_interleaved("cfind_series", fn, REPS)


def request_wipe(k, timeout=300):
    open(os.path.join(DATA_DIR, f"bench-wipe-req-{k}"), "w").close()
    ack = os.path.join(DATA_DIR, f"bench-wipe-ack-{k}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(ack):
            time.sleep(2)  # health said OK; small margin for the DIMSE listener
            return True
        time.sleep(1)
    return False


def bench_cold_rounds():
    for rnd in range(COLD_ROUNDS):
        split = bench_plan.cold_round_split(rnd, 2)
        if not request_wipe(rnd + 1):
            # surface the loss: error rows for every cold cell this round would
            # have produced, so the report shows errors instead of missing data
            record("wipe", "proxy", rnd, 0.0, False, "wipe ack timeout")
            skipped = "skipped: wipe ack timeout"
            for idx, study in enumerate(BIG):
                rep = rnd * 2 + idx
                uid = study["StudyInstanceUID"]
                record("wado_meta_cold", "proxy", rep, 0.0, False, skipped, uid)
                record("wado_meta_cold", "direct", rep, 0.0, False, skipped, uid)
            for name in ("cmove_cold", "wado_frame_cold"):
                for idx in split[name]:
                    rep = rnd * 2 + idx
                    uid = BIG[idx]["StudyInstanceUID"]
                    record(name, "proxy", rep, 0.0, False, skipped, uid)
                    record(name, "direct", rep, 0.0, False, skipped, uid)
            continue
        # metadata first: pass-through, does NOT populate the study cache
        for idx, study in enumerate(BIG):
            rep = rnd * 2 + idx
            t_ms, ok, err = timed_http(meta_url(PROXY["http"], study))
            record("wado_meta_cold", "proxy", rep, t_ms, ok, err, study["StudyInstanceUID"])
            t_ms, ok, err = timed_http(meta_url(DIRECT["http"], study))
            record("wado_meta_cold", "direct", rep, t_ms, ok, err, study["StudyInstanceUID"])
        for idx in split["cmove_cold"]:
            study = BIG[idx]
            uid = study["StudyInstanceUID"]
            rep = rnd * 2 + idx
            t_ms, ok, err = timed_cmove(PROXY, uid, BIG_INSTANCES)
            record("cmove_cold", "proxy", rep, t_ms, ok, err, uid)
            t_ms, ok, err = timed_cmove(DIRECT, uid, BIG_INSTANCES)
            record("cmove_cold", "direct", rep, t_ms, ok, err, uid)
        for idx in split["wado_frame_cold"]:
            study = BIG[idx]
            uid = study["StudyInstanceUID"]
            rep = rnd * 2 + idx
            t_ms, ok, err = timed_http(frame_url(PROXY["http"], study))
            record("wado_frame_cold", "proxy", rep, t_ms, ok, err, uid)
            t_ms, ok, err = timed_http(frame_url(DIRECT["http"], study))
            record("wado_frame_cold", "direct", rep, t_ms, ok, err, uid)


def bench_warm_pass():
    # both big studies are cached: each cold round moved one and frame-fetched the other
    def mv(path, rep):
        idx = rep % 2 if rep >= 0 else 0
        uid = BIG[idx]["StudyInstanceUID"]
        t_ms, ok, err = timed_cmove(TARGET[path], uid, BIG_INSTANCES)
        return t_ms, ok, err, uid
    run_interleaved("cmove_warm", mv, MOVE_REPS, warmup=1)

    def meta(path, rep):
        idx = rep % 2 if rep >= 0 else 0
        t_ms, ok, err = timed_http(meta_url(TARGET[path]["http"], BIG[idx]))
        return t_ms, ok, err, BIG[idx]["StudyInstanceUID"]
    run_interleaved("wado_meta_warm", meta, REPS)

    def frame(path, rep):
        idx = rep % 2 if rep >= 0 else 0
        t_ms, ok, err = timed_http(frame_url(TARGET[path]["http"], BIG[idx]))
        return t_ms, ok, err, BIG[idx]["StudyInstanceUID"]
    run_interleaved("wado_frame_warm", frame, REPS)


def bench_qido():
    def fn(path, rep):
        # warm-up reps get indices >= 1000: still unique, never colliding with counted
        q = bench_plan.qido_query(rep if rep >= 0 else 1000 - rep)
        t_ms, ok, err = timed_http(qido_url(TARGET[path]["http"], q))
        return t_ms, ok, err, None
    run_interleaved("qido", fn, REPS)


def bench_qido_warm():
    # prime (uncounted) + immediate identical repeat inside the 5 s TTL (counted);
    # direct column falls back to `qido`'s in the report
    for i in range(REPS):
        url = qido_url(PROXY["http"], bench_plan.qido_query(i, warm=True))
        timed_http(url)
        t_ms, ok, err = timed_http(url)
        record("qido_warm", "proxy", i, t_ms, ok, err)


# --- readiness / sanity ---

def wait_http(url, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


def wait_file(path, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(5)
    return False


def _multi_import_ok():
    """Exactly FIND_STUDIES studies behind the multi patient — import complete."""
    name = urllib.parse.quote(MULTI["PatientName"])
    limit = max(100, FIND_STUDIES + 1)  # +1 so an over-count is still detectable
    url = qido_url(DIRECT["http"], f"PatientName={name}&limit={limit}")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read()
        n = len(json.loads(body) if body else [])  # QIDO empty result = 204, no body
    except Exception as exc:
        return False, repr(exc)
    if n != FIND_STUDIES:
        return False, f"{n} studies for {MULTI['PatientName']}, expected {FIND_STUDIES}"
    return True, None


def sanity():
    """Fail fast with a reason instead of producing an empty report."""
    checks = []
    for name, target in (("direct", DIRECT), ("proxy", PROXY)):
        _t, ok, err = timed_cecho(target)
        checks.append({"check": "cecho_" + name, "ok": ok, "error": err})
        _t, ok, err = timed_http(target["http"] + "/dicom-web/studies?limit=1", timeout=30)
        checks.append({"check": "qido_" + name, "ok": ok, "error": err})
    ok, err = _multi_import_ok()
    checks.append({"check": "bench_multi_import", "ok": ok, "error": err})
    _t, ok, err = timed_http(meta_url(DIRECT["http"], BIG[0]))
    checks.append({"check": "bench_big_meta", "ok": ok, "error": err})
    return checks


def main():
    meta = {"reps": REPS, "move_reps": MOVE_REPS, "cold_rounds": COLD_ROUNDS,
            "big_instances": BIG_INSTANCES, "find_studies": FIND_STUDIES,
            "find_instances": FIND_INSTANCES,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    server = None
    rc = 0
    try:
        server = start_scp()
        proxy_up = wait_http(PROXY["http"] + "/health", 1800)
        pacs_up = wait_http(DIRECT["http"] + "/dicom-web/studies?limit=1", 300)
        if not (proxy_up and pacs_up):
            meta["fatal"] = f"readiness timeout: proxy_up={proxy_up} pacs_up={pacs_up}"
            rc = 1
            return rc
        if not wait_file(os.path.join(DATA_DIR, "pacs-done"), 900):
            meta["fatal"] = "bench data import did not complete (no pacs-done)"
            rc = 1
            return rc
        meta["sanity"] = sanity()
        failed = [c["check"] for c in meta["sanity"] if not c["ok"]]
        if failed:
            meta["fatal"] = f"sanity failed: {', '.join(failed)}"
            rc = 1
            return rc
        bench_cfind_study()
        bench_cfind_series()
        bench_cold_rounds()
        bench_warm_pass()
        bench_qido()
        bench_qido_warm()
        return rc
    finally:
        # stop the SCP first: best-effort teardown of the store threads before
        # finalizing results; the markers below must land regardless
        if server is not None:
            with contextlib.suppress(Exception):
                server.shutdown()
        try:
            meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            tmp = RESULT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"role": "bench", "meta": meta, "samples": samples},
                          f, ensure_ascii=False)
            os.replace(tmp, RESULT_PATH)
        finally:
            open(os.path.join(DATA_DIR, "bench-stop"), "w").close()
            open(os.path.join(DATA_DIR, "bench-done"), "w").close()


if __name__ == "__main__":
    sys.exit(main())
