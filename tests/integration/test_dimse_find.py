from __future__ import annotations

import asyncio

import pytest
from pynetdicom import AE
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    StudyRootQueryRetrieveInformationModelFind,
    Verification,
)


def _dimse_port(ctx) -> int:
    return ctx["cfg"].dimse.listen_port


@pytest.mark.asyncio
async def test_external_scu_c_echo(app_client) -> None:
    _, ctx = app_client
    ae = AE(ae_title="WORKSTATION")
    ae.add_requested_context(Verification)
    assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["pool_aet"])
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
        assoc = ae.associate("127.0.0.1", _dimse_port(ctx), ae_title=ctx["pool_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = ""
        query.PatientName = ""
        found = []
        for status, ident in assoc.send_c_find(
            query, StudyRootQueryRetrieveInformationModelFind
        ):
            if status and status.Status == 0xFF00 and ident is not None:
                found.append(str(ident.StudyInstanceUID))
        assoc.release()
        return found

    found = await asyncio.to_thread(_run_scu)
    assert seeded_study["study"][0] in found
