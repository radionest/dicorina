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
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING, Any

from dimsechord import (
    ArrivalTimeoutError,
    AssociationError,
    FindFailedError,
    MoveToSelfError,
    NoPresentationContextError,
    PoolExhaustedError,
    SeriesQuery,
    StoreSession,
    build_storage_scp_contexts,
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
    """Face AE: QR/Echo + storage SCP contexts, plus storage SCU contexts for C-MOVE forwarding.

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
    for cx in build_storage_scp_contexts():
        ae.add_supported_context(cx.abstract_syntax, cx.transfer_syntax)  # type: ignore[arg-type]
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
        store_aet: str = "",
        store_timeout: float = 30.0,
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
        self._store_aet = store_aet or aet
        self._store_timeout = store_timeout
        self._store_sessions: dict[Any, StoreSession] = {}
        self._store_inflight: set[Any] = set()
        self._store_doomed: set[Any] = set()
        self._store_lock = threading.Lock()
        self._server: Any | None = None
        self._warned_keys: set[str] = set()

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
            (evt.EVT_C_STORE, self._on_store),
            (evt.EVT_RELEASED, self._on_assoc_end),
            (evt.EVT_ABORTED, self._on_assoc_end),
            (evt.EVT_CONN_CLOSE, self._on_assoc_end),
        ]
        self._server = ae.start_server((ip, port), block=False, evt_handlers=handlers)
        logger.info("DIMSE face listening on %s:%s (AET: %s)", ip, port, self._aet)

    def stop(self) -> None:
        """Stop the DIMSE face and close idle store sessions.

        ``ThreadedAssociationServer.shutdown()`` closes the listening socket
        but never joins the per-association threads, so a store can still be
        in flight on its own thread when this runs. Idle sessions close right
        away; in-flight ones are doomed instead — same deferral as
        ``_on_assoc_end`` — so close() never races the store it belongs to.
        """
        if self._server is not None:
            self._server.shutdown()
            self._server = None
            logger.info("DIMSE face stopped")
        with self._store_lock:
            idle = [
                (assoc, session)
                for assoc, session in self._store_sessions.items()
                if assoc not in self._store_inflight
            ]
            for assoc, _ in idle:
                del self._store_sessions[assoc]
            # In-flight stores own their sessions: doom them and let each
            # store's finally do the close once store() returns (same
            # mutual-exclusion rule as _on_assoc_end).
            self._store_doomed.update(self._store_sessions.keys())
        for _, session in idle:
            session.close()

    # ── handlers ──────────────────────────────────────────────────
    @staticmethod
    def _on_echo(event: evt.Event) -> int:  # noqa: ARG004
        return 0x0000

    def _on_store(self, event: evt.Event) -> int:
        """Relay one instance to the PACS; the response status is the PACS's own.

        Runs in the pynetdicom worker thread — same no-event-loop-hop design as
        C-FIND. One StoreSession per inbound association, created on its first
        C-STORE. EVT_CONN_CLOSE fires on pynetdicom's DUL thread and can land
        mid-store, concurrently with this handler; cleanup therefore defers via
        a "doomed" marker (see ``_on_assoc_end``) consumed in the ``finally``
        below, so ``close()`` never runs while ``store()`` is still in flight
        (StoreSession's one-thread-at-a-time contract holds). CONN_CLOSE landing
        before the first store needs nothing extra: pynetdicom queues an
        A-P-ABORT and the late EVT_ABORTED — on the reactor thread, after this
        handler has already returned — pops the entry then.
        """
        assoc = event.assoc
        with self._store_lock:
            session = self._store_sessions.get(assoc)
            if session is None:
                session = StoreSession(
                    self._pacs,
                    calling_aet=self._store_aet,
                    timeout=self._store_timeout,
                )
                self._store_sessions[assoc] = session
            self._store_inflight.add(assoc)
        sop = ""
        try:
            ds = event.dataset
            ds.file_meta = event.file_meta
            sop = str(getattr(ds, "SOPInstanceUID", "") or "")
            return session.store(ds)
        except NoPresentationContextError as e:
            self._warn_once(
                f"store-ctx:{e.sop_class_uid}:{e.transfer_syntax}",
                "C-STORE relay refused: no upstream context for SOP class %s "
                "with transfer syntax %s (sop=%s)",
                e.sop_class_uid,
                e.transfer_syntax,
                sop or "-",
            )
            return 0x0122  # SOP class not supported
        except AssociationError as e:
            logger.error("C-STORE relay failed [%s] (sop=%s): %s", type(e).__name__, sop or "-", e)
            return 0xA700  # Out of resources
        except Exception:
            logger.exception("C-STORE relay failed (sop=%s)", sop or "-")
            return 0xC000
        finally:
            with self._store_lock:
                self._store_inflight.discard(assoc)
                doomed = assoc in self._store_doomed
                if doomed:
                    self._store_doomed.discard(assoc)
                    if self._store_sessions.get(assoc) is session:
                        del self._store_sessions[assoc]
            if doomed:
                session.close()

    def _on_assoc_end(self, event: evt.Event) -> None:
        """Close the store session when its inbound association ends.

        Registered for RELEASED, ABORTED and CONN_CLOSE — more than one can fire
        for the same association; pop makes the close idempotent. CONN_CLOSE runs
        on the DUL thread and can land while ``_on_store`` is still mid-store for
        this same association (a different thread) — in that case, defer: mark
        the association doomed and let ``_on_store``'s own ``finally`` pop and
        close once ``store()`` has returned, so the two never race.
        """
        with self._store_lock:
            if event.assoc in self._store_inflight:
                self._store_doomed.add(event.assoc)
                return
            session = self._store_sessions.pop(event.assoc, None)
        if session is not None:
            session.close()

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

    def _warn_once(self, key: str, msg: str, *args: object) -> None:
        """First occurrence per key logs WARNING; repeats drop to DEBUG (log-spam guard)."""
        if key in self._warned_keys:
            logger.debug(msg, *args)
            return
        self._warned_keys.add(key)
        logger.warning(msg, *args)

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
        if not matching and results:
            self._warn_once(
                "series-fallback",
                "backend PACS returned %d series-level C-FIND results, none matching "
                "series=%s (study=%s); falling back to the unfiltered total",
                len(results),
                series,
                study,
            )
            matching = results
        elif len(matching) < len(results):
            self._warn_once(
                "series-filter",
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
        matching = [r for r in results if r.study_instance_uid == study]
        if not matching and results:
            self._warn_once(
                "study-fallback",
                "backend PACS returned %d series-level C-FIND results, none matching "
                "study=%s; falling back to the unfiltered total",
                len(results),
                study,
            )
            matching = results
        elif len(matching) < len(results):
            self._warn_once(
                "study-filter",
                "backend PACS ignored StudyInstanceUID matching key: %d of %d "
                "series-level C-FIND results match study=%s; "
                "counting matching results only",
                len(matching),
                len(results),
                study,
            )
        series_uids = [r.series_instance_uid for r in matching]
        count = sum((r.number_of_series_related_instances or 0) for r in matching)
        return count, self._engine.iter_study(study, series_uids)
