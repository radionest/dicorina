from __future__ import annotations

import asyncio
import time

import pytest
from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    PatientRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelFind,
    Verification,
)

from tests.factories import make_instance


def _dimse_port(ctx) -> int:
    return ctx["cfg"].dimse.listen_port


@pytest.mark.asyncio
async def test_external_scu_c_echo(app_client) -> None:
    _, ctx = app_client
    ae = AE(ae_title="WORKSTATION")
    ae.add_requested_context(Verification)
    assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
    assert assoc.is_established
    status = assoc.send_c_echo()
    assert status.Status == 0x0000
    assoc.release()


@pytest.mark.asyncio
async def test_external_scu_c_find_study(app_client, seeded_study) -> None:
    _, ctx = app_client
    from pydicom.dataset import Dataset

    def _run_scu() -> list[str]:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = ""
        query.PatientName = ""
        found = []
        for status, ident in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind):
            if status and status.Status == 0xFF00 and ident is not None:
                found.append(str(ident.StudyInstanceUID))
        assoc.release()
        return found

    found = await asyncio.to_thread(_run_scu)
    assert seeded_study["study"][0] in found


@pytest.mark.asyncio
async def test_cfind_forwards_query_keys(app_client, fake_pacs) -> None:
    _, ctx = app_client

    def _run_scu() -> None:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.SpecificCharacterSet = "ISO_IR 192"
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = ""
        query.PatientName = "Иванов*"
        query.StudyDate = "20260101-20260201"
        list(assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind))
        assoc.release()

    await asyncio.to_thread(_run_scu)
    seen = fake_pacs.find_identifiers[-1]
    assert str(seen.PatientName) == "Иванов*"
    assert str(seen.StudyDate) == "20260101-20260201"


@pytest.mark.asyncio
async def test_cfind_streams_responses_progressively(app_client, fake_pacs) -> None:
    _, ctx = app_client
    for i in range(1, 3):
        fake_pacs.add_instance(
            make_instance(f"1.2.900.{i}", f"1.2.900.{i}.1", f"1.2.900.{i}.1.1")
        )
    fake_pacs.find_response_delay = 0.4

    def _run_scu() -> list[float]:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = ""
        stamps: list[float] = []
        for status, ident in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind):
            if status and status.Status in (0xFF00, 0xFF01) and ident is not None:
                stamps.append(time.monotonic())
        assoc.release()
        return stamps

    stamps = await asyncio.to_thread(_run_scu)
    assert len(stamps) == 3
    assert stamps[-1] - stamps[0] >= 0.6  # first response long before the last


@pytest.mark.asyncio
async def test_cfind_patient_level_passes_through(app_client, fake_pacs) -> None:
    _, ctx = app_client

    def _run_scu() -> int:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(PatientRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "PATIENT"
        query.PatientID = ""
        final = 0xFFFF
        for status, _ident in assoc.send_c_find(
            query, PatientRootQueryRetrieveInformationModelFind
        ):
            if status:
                final = int(status.Status)
        assoc.release()
        return final

    assert await asyncio.to_thread(_run_scu) == 0x0000
    assert str(fake_pacs.find_identifiers[-1].QueryRetrieveLevel) == "PATIENT"


@pytest.mark.asyncio
async def test_cfind_forwards_pacs_failure_status(app_client, fake_pacs) -> None:
    _, ctx = app_client
    fake_pacs.fail_find_with = 0xA900

    def _run_scu() -> list[int]:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = ""
        codes = [
            int(s.Status)
            for s, _ in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind)
            if s
        ]
        assoc.release()
        return codes

    assert 0xA900 in await asyncio.to_thread(_run_scu)


@pytest.mark.asyncio
async def test_cfind_mid_stream_failure_forwards_status(app_client, fake_pacs) -> None:
    _, ctx = app_client
    fake_pacs.fail_find_with = 0xA900
    fake_pacs.fail_find_after = 1

    def _run_scu() -> list[int]:
        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = ""
        codes = [
            int(s.Status)
            for s, _ in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind)
            if s
        ]
        assoc.release()
        return codes

    codes = await asyncio.to_thread(_run_scu)
    assert 0xFF00 in codes  # at least one result delivered before the failure
    assert codes[-1] == 0xA900  # transparent PACS status forward mid-stream
