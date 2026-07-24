"""End-to-end DIMSE C-STORE relay through the live app against FakePacs
(spec: dimse-store-relay — status relay + identity scenarios)."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydicom.uid import MRImageStorage, generate_uid
from pynetdicom import AE

from tests.factories import make_instance


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
