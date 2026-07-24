"""End-to-end DIMSE C-STORE relay through the live app against FakePacs
(spec: dimse-store-relay — status relay + identity scenarios)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from pydicom.dataset import Dataset
from pydicom.uid import JPEGLSLossless, MRImageStorage, generate_uid
from pynetdicom import AE
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,  # type: ignore[attr-defined]
)

from tests.factories import make_compressed_instance, make_instance


def _dimse_port(ctx) -> int:
    return ctx["cfg"].dimse.listen_port


def _new_instances(n: int) -> list:
    study, series = generate_uid(), generate_uid()
    return [make_instance(study, series, generate_uid()) for _ in range(n)]


def _store(ctx, instances, *, contexts=((MRImageStorage, None),)) -> list[int]:
    ae = AE(ae_title="WORKSTATION")
    for sop_class, ts in contexts:
        if ts is None:
            ae.add_requested_context(sop_class)
        else:
            ae.add_requested_context(sop_class, ts)
    assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
    assert assoc.is_established
    try:
        return [int(assoc.send_c_store(ds).Status) for ds in instances]
    finally:
        assoc.release()


@pytest.mark.asyncio
async def test_store_relays_to_pacs(app_client, fake_pacs) -> None:
    _, ctx = app_client
    instances = _new_instances(2)
    statuses = await asyncio.to_thread(_store, ctx, instances)
    assert statuses == [0x0000, 0x0000]
    got = {str(ds.SOPInstanceUID) for ds in fake_pacs.stored}
    assert got == {str(ds.SOPInstanceUID) for ds in instances}


@pytest.mark.asyncio
async def test_store_warning_status_passes_through(app_client, fake_pacs) -> None:
    _, ctx = app_client
    fake_pacs.store_status = 0xB000
    statuses = await asyncio.to_thread(_store, ctx, _new_instances(1))
    assert statuses == [0xB000]
    assert len(fake_pacs.stored) == 1


@pytest.mark.asyncio
async def test_store_failure_status_passes_through(app_client, fake_pacs) -> None:
    _, ctx = app_client
    fake_pacs.store_status = 0xA900
    statuses = await asyncio.to_thread(_store, ctx, _new_instances(1))
    assert statuses == [0xA900]


@pytest.mark.asyncio
async def test_store_default_calling_aet_is_face_aet(app_client, fake_pacs) -> None:
    _, ctx = app_client
    await asyncio.to_thread(_store, ctx, _new_instances(1))
    assert fake_pacs.store_calling_aets == [ctx["face_aet"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("app_client", [{"pacs": {"store_aet": "DICSTORE"}}], indirect=True)
async def test_store_calling_aet_override(app_client, fake_pacs) -> None:
    _, ctx = app_client
    await asyncio.to_thread(_store, ctx, _new_instances(1))
    assert fake_pacs.store_calling_aets == ["DICSTORE"]


@pytest.mark.asyncio
async def test_store_no_early_ack(app_client, fake_pacs) -> None:
    fake_pacs.store_response_delay = 1.0
    _, ctx = app_client
    t0 = time.monotonic()
    statuses = await asyncio.to_thread(_store, ctx, _new_instances(1))
    elapsed = time.monotonic() - t0
    assert statuses == [0x0000]
    assert elapsed >= 1.0  # client status waits for the upstream status


def _wait_until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.mark.asyncio
async def test_find_only_association_opens_no_store_session(app_client, fake_pacs) -> None:
    _, ctx = app_client

    def _find_only() -> None:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = ""
        list(assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind))
        assoc.release()

    await asyncio.to_thread(_find_only)
    assert ctx["face_aet"] not in fake_pacs.established_aets


@pytest.mark.asyncio
async def test_store_session_closes_on_release(app_client, fake_pacs) -> None:
    _, ctx = app_client
    before = fake_pacs.active_associations
    await asyncio.to_thread(_store, ctx, _new_instances(1))  # _store releases
    assert _wait_until(lambda: fake_pacs.active_associations == before)


@pytest.mark.asyncio
async def test_store_session_closes_on_abort(app_client, fake_pacs) -> None:
    _, ctx = app_client
    before = fake_pacs.active_associations

    def _store_then_abort() -> None:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(MRImageStorage)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        assoc.send_c_store(_new_instances(1)[0])
        assoc.abort()

    await asyncio.to_thread(_store_then_abort)
    assert _wait_until(lambda: fake_pacs.active_associations == before)


@pytest.mark.asyncio
async def test_no_upstream_context_yields_0122_then_recovers(app_client, fake_pacs) -> None:
    # Default FakePacs accepts uncompressed only -> JPEG-LS instance has no
    # upstream context (0x0122); the SAME inbound association then stores an
    # uncompressed instance fine (spec: association stays usable).
    _, ctx = app_client
    study, series = generate_uid(), generate_uid()
    compressed = make_compressed_instance(study, series, generate_uid())
    plain = make_instance(study, series, generate_uid())
    statuses = await asyncio.to_thread(
        _store,
        ctx,
        [compressed, plain],
        contexts=((MRImageStorage, None), (MRImageStorage, JPEGLSLossless)),
    )
    assert statuses == [0x0122, 0x0000]
    assert [str(d.SOPInstanceUID) for d in fake_pacs.stored] == [str(plain.SOPInstanceUID)]


@pytest.mark.asyncio
async def test_upstream_unreachable_yields_a700(app_client, fake_pacs) -> None:
    _, ctx = app_client
    fake_pacs.stop()
    statuses = await asyncio.to_thread(_store, ctx, _new_instances(1))
    assert statuses == [0xA700]


@pytest.mark.asyncio
async def test_store_survives_upstream_restart(app_client, fake_pacs) -> None:
    _, ctx = app_client

    def _two_stores_with_restart() -> list[int]:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(MRImageStorage)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        try:
            first = int(assoc.send_c_store(_new_instances(1)[0]).Status)
            fake_pacs.stop()
            fake_pacs.start(fake_pacs.port)  # same port; recorder lists persist
            second = int(assoc.send_c_store(_new_instances(1)[0]).Status)
            return [first, second]
        finally:
            assoc.release()

    statuses = await asyncio.to_thread(_two_stores_with_restart)
    assert statuses == [0x0000, 0x0000]
    assert len(fake_pacs.stored) == 2


@pytest.mark.asyncio
async def test_concurrent_stores_bypass_pool_cap(app_client, fake_pacs) -> None:  # noqa: ARG001
    # per_aet_cap=1 in the app_client config: if store sessions leased from the
    # pool, two concurrent storing clients could not both proceed.
    _, ctx = app_client
    results: list[list[int]] = [[], []]
    gate = threading.Barrier(2)

    def _one(slot: int) -> None:
        gate.wait(timeout=10)
        results[slot] = _store(ctx, _new_instances(3))

    t1 = threading.Thread(target=_one, args=(0,))
    t2 = threading.Thread(target=_one, args=(1,))
    t1.start(), t2.start()
    await asyncio.to_thread(t1.join)
    await asyncio.to_thread(t2.join)
    assert results[0] == [0x0000] * 3
    assert results[1] == [0x0000] * 3


@pytest.mark.asyncio
async def test_compressed_instance_passes_through_verbatim(app_client, fake_pacs) -> None:
    _, ctx = app_client
    fake_pacs.stop()
    fake_pacs.store_compressed = True
    fake_pacs.start(fake_pacs.port)
    ds = make_compressed_instance(generate_uid(), generate_uid(), generate_uid())
    statuses = await asyncio.to_thread(
        _store, ctx, [ds], contexts=((MRImageStorage, JPEGLSLossless),)
    )
    assert statuses == [0x0000]
    assert fake_pacs.store_transfer_syntaxes == [str(JPEGLSLossless)]
    assert str(fake_pacs.stored[0].SOPInstanceUID) == str(ds.SOPInstanceUID)
