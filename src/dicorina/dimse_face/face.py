"""DIMSE-SCP face: a pynetdicom AE wrapping the dimsechord core.

C-FIND is a pure sync pass-through: the pynetdicom worker thread iterates
QueryEngine.iter_find directly (no event loop hop) and forwards raw
identifiers 1:1. C-MOVE planning still reaches the async DicomClient via
``run_coroutine_threadsafe``; C-MOVE data consumes the synchronous
PullEngine iterators directly.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING, Any

from dimsechord import (
    ArrivalTimeoutError,
    AssociationError,
    FindFailedError,
    MoveToSelfError,
    PoolExhaustedError,
    SeriesQuery,
    build_storage_scu_contexts,
)
from pynetdicom import AE, evt
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dimsechord import DicomClient, DicomNode, PullEngine, QueryEngine
    from pydicom import Dataset

    from dicorina.dimse_face.allowlist import DestinationAllowlist

logger = logging.getLogger(__name__)


def _build_ae(aet: str) -> AE:
    """Face AE: QR/Echo SCP contexts + storage SCU contexts for C-MOVE forwarding.

    Requested contexts come from dimsechord's builder: one uncompressed context
    per storage class plus one context per (image class x compressed TS), so the
    sub-association can C-STORE cached instances verbatim in their original
    transfer syntax (pass-through D7). pynetdicom's defaults are uncompressed-only.
    """
    ae = AE(ae_title=aet)
    ae.require_called_aet = True
    for cx in (
        Verification,
        PatientRootQueryRetrieveInformationModelFind,
        StudyRootQueryRetrieveInformationModelFind,
        PatientRootQueryRetrieveInformationModelMove,
        StudyRootQueryRetrieveInformationModelMove,
    ):
        ae.add_supported_context(cx)
    ae.requested_contexts = build_storage_scu_contexts()
    return ae


class DimseFace:
    def __init__(
        self,
        engine: PullEngine,
        client: DicomClient,
        query: QueryEngine,
        pacs: DicomNode,
        allowlist: DestinationAllowlist,
        loop: asyncio.AbstractEventLoop,
        aet: str,
        *,
        cfind_timeout: float = 30.0,
        cmove_count_timeout: float = 30.0,
    ) -> None:
        self._engine = engine
        self._client = client
        self._query = query
        self._pacs = pacs
        self._allowlist = allowlist
        self._loop = loop
        self._aet = aet
        self._cfind_timeout = cfind_timeout
        self._cmove_count_timeout = cmove_count_timeout
        self._server: Any | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def start(self, port: int, ip: str = "0.0.0.0") -> None:
        if self._server is not None:
            return
        # The external face accepts only cfg.dimse.aet as called-AET; the pool
        # holds upstream identities and no longer names the face.
        ae = _build_ae(self._aet)
        handlers: list[Any] = [
            (evt.EVT_C_ECHO, self._on_echo),
            (evt.EVT_C_FIND, self._on_find),
            (evt.EVT_C_MOVE, self._on_move),  # implemented in Task 8
        ]
        self._server = ae.start_server((ip, port), block=False, evt_handlers=handlers)
        logger.info("DIMSE face listening on %s:%s (AET: %s)", ip, port, self._aet)

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
        wall_clock = self._cfind_timeout + 5.0
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=wall_clock)
        except FuturesTimeout as exc:
            if future.done():
                # The future finished — either it completed successfully in the
                # race window between future.result timing out and this check
                # (return its result), or the coroutine raised its own exception
                # (re-raise it verbatim). Either way, future.result() here never
                # raises the blank-message wall-clock FuturesTimeout.
                return future.result()
            # future.result timed out with the coroutine still running; its str() is "".
            raise TimeoutError(
                f"upstream DICOM call did not finish within {wall_clock:.0f}s wall-clock "
                f"(PACS slow or returned too many results)"
            ) from exc

    def _on_find(self, event: evt.Event) -> Iterator[tuple[int, Dataset | None]]:
        ident = event.identifier
        # For log context only — the raw identifier passes through to iter_find untouched.
        level = str(getattr(ident, "QueryRetrieveLevel", "") or "")
        study = str(getattr(ident, "StudyInstanceUID", "") or "")
        series = str(getattr(ident, "SeriesInstanceUID", "") or "")
        model = event.context.abstract_syntax  # Patient/Study Root, as negotiated
        gen = self._query.iter_find(ident, model=model, timeout=self._cfind_timeout)
        try:
            for ds in gen:
                if event.is_cancelled:
                    yield (0xFE00, None)  # upstream released in finally
                    return
                yield (0xFF00, ds)  # same SCP thread, no event loop hop
        except (PoolExhaustedError, AssociationError) as e:
            logger.error(
                "DIMSE C-FIND refused [%s] (level=%s study=%s series=%s): %s",
                type(e).__name__,
                level or "-",
                study or "-",
                series or "-",
                e,
            )
            yield (0xA700, None)  # Refused: Out of Resources
            return
        except FindFailedError as e:
            logger.error(
                "DIMSE C-FIND upstream failure [%s] (level=%s study=%s series=%s): %s",
                type(e).__name__,
                level or "-",
                study or "-",
                series or "-",
                e,
            )
            yield (e.status, None)  # transparent PACS status forward
            return
        except Exception as e:
            logger.exception(
                "DIMSE C-FIND failed [%s] (level=%s study=%s series=%s)",
                type(e).__name__,
                level or "-",
                study or "-",
                series or "-",
            )
            yield (0xC000, None)
            return
        finally:
            # break/close/GeneratorExit → upstream abort + find-lease release,
            # deterministic instead of waiting on GC (mirrors the HTTP path).
            gen.close()  # type: ignore[attr-defined]
        yield (0x0000, None)

    def _on_move(self, event: evt.Event) -> Iterator[Any]:
        ident = event.identifier
        level = str(getattr(ident, "QueryRetrieveLevel", "STUDY"))
        study = str(getattr(ident, "StudyInstanceUID", "") or "")
        series = str(getattr(ident, "SeriesInstanceUID", "") or "")

        dest_raw = event.move_destination
        dest_aet = (
            dest_raw.decode().strip() if isinstance(dest_raw, bytes) else str(dest_raw).strip()
        )
        dest = self._allowlist.resolve(dest_aet)
        if dest is None:
            logger.warning("C-MOVE to unknown destination AET %r refused", dest_aet)
            yield (None, None)  # → 0xA801 Move Destination unknown
            return
        yield (dest.host, dest.port)

        # Sub-operation count from series-level C-FIND (never instance-level).
        try:
            if level == "SERIES" and series:
                count, iterator = self._series_move(study, series)
            else:
                count, iterator = self._study_move(study)
        except Exception as e:
            logger.exception("C-MOVE planning failed [%s] for study=%s", type(e).__name__, study)
            yield 0
            yield (0xA702, None)  # Unable to perform sub-operations
            return
        yield count

        try:
            for ds in iterator:
                if event.is_cancelled:
                    yield (0xFE00, None)
                    return
                yield (0xFF00, ds)
        except (
            MoveToSelfError,
            ArrivalTimeoutError,
            AssociationError,
            PoolExhaustedError,
        ) as e:
            logger.exception(
                "C-MOVE pass-through failed [%s] for study=%s", type(e).__name__, study
            )
            yield (0xA702, None)
            return

    def _series_move(self, study: str, series: str) -> tuple[int, Iterator[Dataset]]:
        results = self._run(
            self._client.find_series(
                SeriesQuery(study_instance_uid=study, series_instance_uid=series),
                self._pacs,
                timeout=self._cfind_timeout,
            )
        )
        matching = [r for r in results if r.series_instance_uid == series]
        if len(matching) != len(results):
            logger.warning(
                "backend PACS ignored SeriesInstanceUID matching key: %d of %d "
                "series-level C-FIND results match series=%s (study=%s); "
                "counting matching results only",
                len(matching),
                len(results),
                series,
                study,
            )
        count = sum((r.number_of_series_related_instances or 0) for r in matching)
        return count, self._engine.iter_series(study, series)

    def _study_move(self, study: str) -> tuple[int, Iterator[Dataset]]:
        results = self._run(
            self._client.find_series(
                SeriesQuery(study_instance_uid=study), self._pacs, timeout=self._cfind_timeout
            )
        )
        series_uids = [r.series_instance_uid for r in results]
        count = sum((r.number_of_series_related_instances or 0) for r in results)
        return count, self._engine.iter_study(study, series_uids)
