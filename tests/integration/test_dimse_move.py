from __future__ import annotations

import asyncio
import time

import pytest
from pynetdicom import AE, evt
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    StudyRootQueryRetrieveInformationModelMove,
)


class _Receiver:
    """A throwaway C-STORE SCP acting as the C-MOVE destination modality."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self._server = None

    def start(self, port: int, aet: str) -> None:
        from pynetdicom import StoragePresentationContexts

        ae = AE(ae_title=aet)
        for cx in StoragePresentationContexts:
            if cx.abstract_syntax is not None:
                ae.add_supported_context(cx.abstract_syntax)
        self._server = ae.start_server(
            ("127.0.0.1", port), block=False, evt_handlers=[(evt.EVT_C_STORE, self._store)]
        )

    def _store(self, event: evt.Event) -> int:
        self.received.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()


@pytest.mark.asyncio
async def test_cmove_passthrough_to_registered_modality(
    app_client, seeded_study, free_port
) -> None:
    _, ctx = app_client
    study = seeded_study["study"][0]
    expected = {sop for s in seeded_study["series"] for sop in seeded_study[s]}

    recv_port = free_port()
    recv = _Receiver()
    recv.start(recv_port, "WORKSTATION")
    from dicorina.dimse_face.allowlist import Destination

    ctx["app"].state.dimse._allowlist._dests["WORKSTATION"] = Destination("127.0.0.1", recv_port)

    def _run_scu() -> list:
        from pydicom.dataset import Dataset

        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
        assoc = ae.associate("127.0.0.1", ctx["cfg"].dimse.listen_port, ae_title=ctx["face_aet"])
        assert assoc.is_established

        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = study
        statuses = list(
            assoc.send_c_move(query, "WORKSTATION", StudyRootQueryRetrieveInformationModelMove)
        )
        assoc.release()

        deadline = time.time() + 15
        while len(recv.received) < len(expected) and time.time() < deadline:
            time.sleep(0.2)
        return statuses

    try:
        statuses = await asyncio.to_thread(_run_scu)
        assert set(recv.received) == expected
        assert any(s.Status == 0x0000 for s, _ in statuses if s is not None)
    finally:
        recv.stop()


@pytest.mark.asyncio
async def test_cmove_unknown_destination_refused(app_client, seeded_study) -> None:
    _, ctx = app_client
    study = seeded_study["study"][0]

    def _run_scu() -> list[int]:
        from pydicom.dataset import Dataset

        ae = AE(ae_title="GHOST")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
        assoc = ae.associate("127.0.0.1", ctx["cfg"].dimse.listen_port, ae_title=ctx["face_aet"])
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = study
        statuses = [
            s.Status
            for s, _ in assoc.send_c_move(
                query, "GHOST", StudyRootQueryRetrieveInformationModelMove
            )
            if s
        ]
        assoc.release()
        return statuses

    statuses = await asyncio.to_thread(_run_scu)
    assert 0xA801 in statuses  # Move Destination unknown


@pytest.mark.asyncio
async def test_series_cmove_counters_against_match_widening_backend(
    app_client, seeded_study, fake_pacs, free_port
) -> None:
    """Pending C-MOVE-RSP sub-operation counters must reflect the requested series,
    not the whole study, even when the backend PACS ignores the SeriesInstanceUID
    matching key at series level (#21). Delivery must stay series-exact."""
    _, ctx = app_client
    study = seeded_study["study"][0]
    series = seeded_study["series"][0]
    expected = set(seeded_study[series])
    fake_pacs.widen_series_find = True

    recv_port = free_port()
    recv = _Receiver()
    recv.start(recv_port, "WORKSTATION")
    from dicorina.dimse_face.allowlist import Destination

    ctx["app"].state.dimse._allowlist._dests["WORKSTATION"] = Destination("127.0.0.1", recv_port)

    def _run_scu() -> list:
        from pydicom.dataset import Dataset

        ae = AE(ae_title="WORKSTATION")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
        assoc = ae.associate("127.0.0.1", ctx["cfg"].dimse.listen_port, ae_title=ctx["face_aet"])
        assert assoc.is_established

        query = Dataset()
        query.QueryRetrieveLevel = "SERIES"
        query.StudyInstanceUID = study
        query.SeriesInstanceUID = series
        statuses = list(
            assoc.send_c_move(query, "WORKSTATION", StudyRootQueryRetrieveInformationModelMove)
        )
        assoc.release()

        deadline = time.time() + 15
        while len(recv.received) < len(expected) and time.time() < deadline:
            time.sleep(0.2)
        return statuses

    try:
        statuses = await asyncio.to_thread(_run_scu)
        pending = [s for s, _ in statuses if s is not None and s.Status == 0xFF00]
        assert pending, "expected at least one pending C-MOVE-RSP"
        for s in pending:
            total = (
                getattr(s, "NumberOfRemainingSuboperations", 0)
                + getattr(s, "NumberOfCompletedSuboperations", 0)
                + getattr(s, "NumberOfFailedSuboperations", 0)
                + getattr(s, "NumberOfWarningSuboperations", 0)
            )
            assert total == len(expected)
        assert any(s.Status == 0x0000 for s, _ in statuses if s is not None)
        assert set(recv.received) == expected
    finally:
        recv.stop()
