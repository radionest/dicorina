"""SCU client agent: Storage SCP + scripted HTTP/DIMSE scenarios S0-S7.

Driven entirely by env (see plan Task 8 interfaces). Records every observation
via agent_core and writes RESULT_PATH; coordinates concurrent phases through
the 9p barrier dir. Runs on Python 3.7 inside the client golden VM."""

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
import agent_core as ac
import study_plan

# --- env (replaces orthanc-proxy's PROXY_AET/PROXY_REST names) ---
ROLE = os.environ["ROLE"]
SELF_AET = os.environ["SELF_AET"]
SCP_PORT = int(os.environ["SCP_PORT"])
PROXY_HOST = os.environ["PROXY_HOST"]
PROXY_DIMSE = int(os.environ["PROXY_DIMSE"])
PROXY_HTTP = int(os.environ["PROXY_HTTP"])
PROXY_CALLED_AET = os.environ["PROXY_CALLED_AET"]   # = DICORINA1 (pool.members[0].aet)
PACS_HOST = os.environ["PACS_HOST"]
PACS_AET = os.environ["PACS_AET"]
PACS_DICOM = int(os.environ["PACS_DICOM"])
BARRIER_DIR = os.environ["BARRIER_DIR"]
RESULT_PATH = os.environ["RESULT_PATH"]
N = int(os.environ.get("INSTANCES_PER_STUDY", "50"))

PLAN = study_plan.build_study_plan(int(os.environ.get("STUDIES", "6")), N)
STUDY = {i + 1: s for i, s in enumerate(PLAN)}   # STUDY[1]..STUDY[6], each a plan dict
STUDY7 = study_plan.build_study_plan(7, N)[6]  # never seeded on the PACS (STUDIES=6)
result = ac.new_result(ROLE, SELF_AET)

_current_phase = {"name": "idle"}
_lock = threading.Lock()


def _on_store(event):
    ds = event.dataset
    ds.file_meta = event.file_meta
    calling = event.assoc.requestor.ae_title
    if hasattr(calling, "decode"):
        calling = calling.decode().strip()
    with _lock:
        ac.record_received(result, _current_phase["name"], str(ds.StudyInstanceUID), str(calling))
    return 0x0000


def drain(phase, study, n, timeout=120):
    """Wait until all n instances of `study` for `phase` have been recorded before the
    caller switches phase, so a late sub-operation can never land in the next phase's bucket.
    Record a `drain` event (ok=False on timeout) so a silent shortfall surfaces to the host gate."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            got = ac.received_count(result, phase, study)
        if got >= n:
            ac.record_event(result, "drain", phase=phase, study=study, got=got, n=n, ok=True)
            return True
        time.sleep(0.5)
    with _lock:
        got = ac.received_count(result, phase, study)
    ac.record_event(result, "drain", phase=phase, study=study, got=got, n=n, ok=False)
    return False


def start_scp():
    ae = AE(ae_title=SELF_AET)
    ae.supported_contexts = StoragePresentationContexts
    return ae.start_server(
        ("0.0.0.0", SCP_PORT), block=False, evt_handlers=[(evt.EVT_C_STORE, _on_store)]
    )


def probe_rejected(host, port, called_aet, calling_aet, kind):
    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(Verification)
    assoc = ae.associate(host, port, ae_title=called_aet)
    rejected = not assoc.is_established
    if assoc.is_established:
        assoc.release()
    ac.record_event(result, kind, rejected=rejected)


def wait_ready(url, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    ac.record_event(result, "proxy_ready", waited=True)
                    return True
        except Exception:
            pass
        time.sleep(5)
    ac.record_event(result, "proxy_ready", waited=False)
    return False


def barrier(phase, mine, names):
    """Signal `mine`, wait for all `names`, and record whether the rendezvous succeeded.
    A timeout means the concurrent phase ran non-concurrently — recorded for the host gate."""
    ac.barrier_signal(BARRIER_DIR, mine)
    ok = ac.barrier_wait_all(BARRIER_DIR, names, timeout=1800)
    ac.record_event(result, "barrier", phase=phase, ok=ok)
    return ok


def _get_json(path, retries=6, timeout=15):
    url = f"http://{PROXY_HOST}:{PROXY_HTTP}{path}"
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                if r.status == 200:
                    return json.loads(r.read())
        except Exception:
            pass
        time.sleep(2)
    return None


def _get_bytes(path, timeout=20):
    url = f"http://{PROXY_HOST}:{PROXY_HTTP}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read() if r.status == 200 else b""
    except Exception:
        return b""


def _qido_name(dataset_list):
    # DICOM-JSON: PatientName tag 00100010 -> {"vr":"PN","Value":[{"Alphabetic": "..."}]}
    if not dataset_list:
        return None
    pn = dataset_list[0].get("00100010", {}).get("Value", [{}])
    return pn[0].get("Alphabetic") if pn else None


def cmove(study_uid, dest_aet):
    ae = AE(ae_title=SELF_AET)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
    assoc = ae.associate(PROXY_HOST, PROXY_DIMSE, ae_title=PROXY_CALLED_AET)
    statuses = []
    if assoc.is_established:
        ds = Dataset()
        ds.QueryRetrieveLevel = "STUDY"
        ds.StudyInstanceUID = study_uid
        for status, _ in assoc.send_c_move(
            ds, dest_aet, StudyRootQueryRetrieveInformationModelMove
        ):
            if status:
                statuses.append(int(status.Status))
        assoc.release()
    return statuses


def _make_s8_instance(st, sop_uid, idx):
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid
    ds = Dataset()
    ds.PatientID = "S8"
    ds.PatientName = "STORE^RELAY"
    ds.StudyInstanceUID = st["StudyInstanceUID"]
    ds.SeriesInstanceUID = st["SeriesInstanceUID"]
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"  # MR Image Storage
    ds.Modality = "MR"
    ds.SeriesNumber = 1
    ds.InstanceNumber = idx + 1
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(16)
    ds.file_meta = Dataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.ImplementationClassUID = generate_uid()
    return ds


def store_study(st):
    ae = AE(ae_title=SELF_AET)
    ae.requested_contexts = StoragePresentationContexts
    assoc = ae.associate(PROXY_HOST, PROXY_DIMSE, ae_title=PROXY_CALLED_AET)
    statuses = []
    if assoc.is_established:
        for i, sop in enumerate(st["SOPInstanceUIDs"]):
            status = assoc.send_c_store(_make_s8_instance(st, sop, i))
            statuses.append(int(status.Status) if status else -1)
        assoc.release()
    return statuses


def cmove_ghost(study_uid):
    statuses = cmove(study_uid, "GHOST")
    ac.record_event(result, "cmove_ghost", statuses=statuses, refused=(0xA801 in statuses))


def cfind_cyrillic(study_uid, expect_name):
    ae = AE(ae_title=SELF_AET)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
    assoc = ae.associate(PROXY_HOST, PROXY_DIMSE, ae_title=PROXY_CALLED_AET)
    got = None
    if assoc.is_established:
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = study_uid
        q.PatientName = ""
        for _st, ident in assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind):
            if ident is not None and "PatientName" in ident:
                got = str(ident.PatientName)
        assoc.release()
    ac.record_event(result, "cfind_cyrillic", name=got, ok=(got == expect_name))


def cfind_filtered(expect_study):
    """DIMSE parity of S1's QIDO filter: PatientName wildcard must narrow to one study."""
    ae = AE(ae_title=SELF_AET)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
    assoc = ae.associate(PROXY_HOST, PROXY_DIMSE, ae_title=PROXY_CALLED_AET)
    got = []
    if assoc.is_established:
        q = Dataset()
        q.SpecificCharacterSet = "ISO_IR 192"
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = ""
        q.PatientName = "Иванов*"
        for _st, ident in assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind):
            if ident is not None and "StudyInstanceUID" in ident:
                got.append(str(ident.StudyInstanceUID))
        assoc.release()
    ac.record_event(result, "cfind_filtered", studies=got, ok=(got == [expect_study]))


def qido_chunked_probe():
    # Cache-bust: a bare "/dicom-web/studies" URL shares its QidoResultCache key with
    # qido_list()'s prior call, so within the 5s TTL this would be served as a buffered
    # cache hit (Content-Length, not chunked) regardless of server behavior. A unique
    # param keeps the cache key distinct so this is always a real streamed MISS.
    url = f"http://{PROXY_HOST}:{PROXY_HTTP}/dicom-web/studies?PatientID=__chunkprobe__"
    chunked = False
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            chunked = (r.getheader("Transfer-Encoding") == "chunked")
    except Exception:
        pass
    ac.record_event(result, "qido_chunked", chunked=chunked)


def qido_list():
    data = _get_json("/dicom-web/studies")
    studies = [d.get("0020000D", {}).get("Value", [None])[0] for d in (data or [])]
    ac.record_event(result, "qido_list", studies=[s for s in studies if s], ok=bool(data))
    fdata = _get_json("/dicom-web/studies?PatientName=" + urllib.parse.quote("Иванов*"))
    fstudies = [d.get("0020000D", {}).get("Value", [None])[0] for d in (fdata or [])]
    ac.record_event(result, "qido_filtered", studies=[s for s in fstudies if s], ok=bool(fdata))


def qido_cyrillic():
    s = STUDY[1]["StudyInstanceUID"]
    data = _get_json(f"/dicom-web/studies?StudyInstanceUID={s}")
    ac.record_event(
        result, "qido_cyrillic",
        name=_qido_name(data),
        ok=(_qido_name(data) == study_plan.CYRILLIC_NAME),
    )


def wado(study_idx, kind):
    st = STUDY[study_idx]
    s, se, inst = st["StudyInstanceUID"], st["SeriesInstanceUID"], st["SOPInstanceUIDs"][0]
    meta = _get_json(f"/dicom-web/studies/{s}/metadata")
    count = len(meta) if isinstance(meta, list) else 0
    frame = (
        _get_bytes(f"/dicom-web/studies/{s}/series/{se}/instances/{inst}/frames/1")
        if kind == "wado"
        else b""
    )
    ev = {"study": s, "metadata_count": count, "ok": count == N}
    if kind == "wado":
        ev["frame_bytes"] = len(frame)
        ev["ok"] = count == N and len(frame) > 0
    ac.record_event(result, kind, **ev)


def main():
    server = start_scp()
    try:
        wait_ready(f"http://{PROXY_HOST}:{PROXY_HTTP}/health", 1200)
        if ROLE == "clienta":
            probe_rejected(PACS_HOST, PACS_DICOM, PACS_AET, SELF_AET, "direct_pacs_probe")
            probe_rejected(PROXY_HOST, PROXY_DIMSE, "GHOST", SELF_AET, "spoof_proxy_probe")
            _current_phase["name"] = "s3"
            cmove(STUDY[3]["StudyInstanceUID"], SELF_AET)
            drain("s3", STUDY[3]["StudyInstanceUID"], N)
            cmove_ghost(STUDY[3]["StudyInstanceUID"])
            cfind_cyrillic(STUDY[1]["StudyInstanceUID"], study_plan.CYRILLIC_NAME)
            cfind_filtered(STUDY[1]["StudyInstanceUID"])
            barrier("s5", "a_s5", ["a_s5", "b_s5"])
            _current_phase["name"] = "s5"
            cmove(STUDY[4]["StudyInstanceUID"], SELF_AET)
            drain("s5", STUDY[4]["StudyInstanceUID"], N)
            _current_phase["name"] = "s6"
            cmove(STUDY[6]["StudyInstanceUID"], SELF_AET)
            drain("s6", STUDY[6]["StudyInstanceUID"], N)
            ac.barrier_signal(BARRIER_DIR, "a_s6_warm")
            statuses = store_study(STUDY7)
            ac.record_event(
                result, "store_relay", statuses=statuses,
                ok=(len(statuses) == N and all(s == 0 for s in statuses)),
            )
            ac.barrier_signal(BARRIER_DIR, "a_s8_stored")
        else:  # clientb
            qido_list()
            qido_chunked_probe()
            wado(2, "wado")
            qido_cyrillic()
            barrier("s5", "b_s5", ["a_s5", "b_s5"])
            _current_phase["name"] = "s5"
            cmove(STUDY[5]["StudyInstanceUID"], SELF_AET)
            drain("s5", STUDY[5]["StudyInstanceUID"], N)
            ac.barrier_wait_all(BARRIER_DIR, ["a_s6_warm"], timeout=600)
            wado(6, "wado_cached")
            ac.barrier_wait_all(BARRIER_DIR, ["a_s8_stored"], timeout=900)
            _current_phase["name"] = "s8"
            cmove(STUDY7["StudyInstanceUID"], SELF_AET)
            drain("s8", STUDY7["StudyInstanceUID"], N)
        time.sleep(5)
    finally:
        # shutdown SCP first: stops _on_store mutations so result dict is stable for json.dump
        server.shutdown()
        ac.write_result(RESULT_PATH, result)
        ac.barrier_signal(BARRIER_DIR, ROLE + "_done")


if __name__ == "__main__":
    main()
