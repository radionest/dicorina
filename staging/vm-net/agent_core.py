"""Pure helpers for the client agent: 9p barrier + result recording. No pynetdicom."""

import json
import os
import time


def barrier_signal(barrier_dir, name):
    os.makedirs(barrier_dir, exist_ok=True)
    open(os.path.join(barrier_dir, "ready_" + name), "w").close()


def barrier_wait_all(
    barrier_dir, names, timeout=120, poll=0.5, sleep=time.sleep, clock=time.monotonic
):
    deadline = clock() + timeout
    while clock() < deadline:
        if all(os.path.exists(os.path.join(barrier_dir, "ready_" + n)) for n in names):
            return True
        sleep(poll)
    return False


def new_result(role, aet):
    return {"role": role, "aet": aet, "received": {}, "events": []}


def record_received(result, phase, study_uid, calling_aet):
    phase_bucket = result["received"].setdefault(phase, {})
    entry = phase_bucket.setdefault(study_uid, {"count": 0, "from": calling_aet})
    entry["count"] += 1
    entry["from"] = calling_aet


def record_event(result, kind, **fields):
    result["events"].append(dict(kind=kind, **fields))


def received_count(result, phase, study_uid):
    return result.get("received", {}).get(phase, {}).get(study_uid, {}).get("count", 0)


def write_result(path, result):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, path)
