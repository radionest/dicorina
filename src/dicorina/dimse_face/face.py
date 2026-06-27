"""DIMSE-SCP face: a pynetdicom AE wrapping the dimsechord core.

C-FIND runs in a pynetdicom worker thread and reaches the async DicomClient via
``run_coroutine_threadsafe`` against the captured uvicorn loop. C-MOVE (Task 8)
consumes the synchronous PullEngine iterators directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from dimsechord import ImageQuery, SeriesQuery, StudyQuery
from pynetdicom import AE, evt
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

from dicorina.dimse_face.results import (
    image_to_dataset,
    series_to_dataset,
    study_to_dataset,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from dimsechord import DicomClient, DicomNode, PullEngine
    from pydicom import Dataset

    from dicorina.dimse_face.allowlist import DestinationAllowlist

logger = logging.getLogger(__name__)


class DimseFace:
    def __init__(
        self,
        engine: PullEngine,
        client: DicomClient,
        pacs: DicomNode,
        allowlist: DestinationAllowlist,
        loop: asyncio.AbstractEventLoop,
        called_aets: set[str],
        *,
        cfind_timeout: float = 30.0,
        cmove_count_timeout: float = 30.0,
    ) -> None:
        self._engine = engine
        self._client = client
        self._pacs = pacs
        self._allowlist = allowlist
        self._loop = loop
        self._called_aets = called_aets
        self._cfind_timeout = cfind_timeout
        self._cmove_count_timeout = cmove_count_timeout
        self._server: Any | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def start(self, port: int, ip: str = "0.0.0.0") -> None:
        if self._server is not None:
            return
        ae = AE(ae_title=next(iter(self._called_aets)))
        ae.require_called_aet = True
        for cx in (
            Verification,
            PatientRootQueryRetrieveInformationModelFind,
            StudyRootQueryRetrieveInformationModelFind,
            PatientRootQueryRetrieveInformationModelMove,
            StudyRootQueryRetrieveInformationModelMove,
        ):
            ae.add_supported_context(cx)
        handlers: list[Any] = [
            (evt.EVT_C_ECHO, self._on_echo),
            (evt.EVT_C_FIND, self._on_find),
            (evt.EVT_C_MOVE, self._on_move),  # implemented in Task 8
        ]
        self._server = ae.start_server((ip, port), block=False, evt_handlers=handlers)
        logger.info("DIMSE face listening on %s:%s (AETs: %s)", ip, port, sorted(self._called_aets))

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
            logger.info("DIMSE face stopped")

    # ── handlers ──────────────────────────────────────────────────
    @staticmethod
    def _on_echo(event: evt.Event) -> int:  # noqa: ARG004
        return 0x0000

    def _run(self, coro: Any) -> Any:
        """Run an async DicomClient call from this pynetdicom worker thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=self._cfind_timeout + 5.0)

    def _on_find(self, event: evt.Event) -> Iterator[tuple[int, Dataset | None]]:
        ident = event.identifier
        level = str(getattr(ident, "QueryRetrieveLevel", "STUDY"))
        study = str(getattr(ident, "StudyInstanceUID", "") or "")
        series = str(getattr(ident, "SeriesInstanceUID", "") or "")
        to_ds: Callable[[Any], Dataset]
        try:
            if level == "STUDY" or not study:
                results = self._run(
                    self._client.find_studies(
                        StudyQuery(study_instance_uid=study or None),
                        self._pacs,
                        timeout=self._cfind_timeout,
                    )
                )
                to_ds = study_to_dataset
            elif level == "SERIES":
                results = self._run(
                    self._client.find_series(
                        SeriesQuery(study_instance_uid=study),
                        self._pacs,
                        timeout=self._cfind_timeout,
                    )
                )
                to_ds = series_to_dataset
            else:  # IMAGE
                results = self._run(
                    self._client.find_images(
                        ImageQuery(study_instance_uid=study, series_instance_uid=series),
                        self._pacs,
                        timeout=self._cfind_timeout,
                    )
                )
                to_ds = image_to_dataset
        except Exception as e:
            logger.error("DIMSE C-FIND failed: %s", e)
            yield (0xC000, None)  # Unable to process
            return
        for r in results:
            if event.is_cancelled:
                yield (0xFE00, None)  # Sub-operations terminated due to Cancel
                return
            yield (0xFF00, to_ds(r))
        yield (0x0000, None)

    def _on_move(self, event: evt.Event) -> Iterator[Any]:  # noqa: ARG002
        yield (None, None)  # Task 8 implements real pass-through
