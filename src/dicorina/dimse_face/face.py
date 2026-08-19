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
import time
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
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

from dicorina.stats import InflightCounter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dimsechord import DicomClient, DicomNode, PullEngine, QueryEngine
    from pydicom import Dataset

    from dicorina.dimse_face.allowlist import DestinationAllowlist

logger = logging.getLogger(__name__)

# A-ASSOCIATE-RJ result/source/reason triples (PS3.8 Table 9-21), decoded so a
# rejection reads as a cause instead of three hex numbers.
_REJECT_RESULT: dict[Any, str] = {0x01: "permanent", 0x02: "transient"}
_REJECT_SOURCE: dict[Any, str] = {
    0x01: "service-user",
    0x02: "service-provider (ACSE)",
    0x03: "service-provider (presentation)",
}
_REJECT_REASON: dict[Any, str] = {
    (0x01, 0x01): "no reason given",
    (0x01, 0x02): "application context name not supported",
    (0x01, 0x03): "calling AE title not recognised",
    (0x01, 0x07): "called AE title not recognised",
    (0x02, 0x01): "no reason given",
    (0x02, 0x02): "protocol version not supported",
    (0x03, 0x01): "temporary congestion",
    (0x03, 0x02): "local limit exceeded",
}


def _peer(assoc: Any) -> str:
    """``AET@host:port`` of the association requestor, for log context."""
    try:
        info = assoc.requestor.info  # a ServiceUser property, not a method
        return f"{info['ae_title']}@{info['address']}:{info['port']}"
    except Exception:
        # Log context must never be the reason a handler fails.
        return "unknown-peer"


@dataclass
class _AssocStats:
    """Per-inbound-association tallies, summarised in one line when it ends."""

    peer: str
    started: float
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def summary(self) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(self.counts.items())) or "no operations"


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
        slow_operation_seconds: float = 10.0,
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
        self._warn_lock = threading.Lock()
        self._slow_seconds = slow_operation_seconds
        self._inflight = InflightCounter()
        self._ae: AE | None = None
        self._assoc_stats: dict[Any, _AssocStats] = {}
        self._assoc_lock = threading.Lock()

    @property
    def inflight(self) -> InflightCounter:
        return self._inflight

    def association_load(self) -> tuple[int, int]:
        """``(inbound associations alive, pynetdicom's cap)``.

        Once the first number reaches the second, pynetdicom answers every
        further association request with an A-ASSOCIATE-RJ before any handler
        of ours runs — which is what clients report as "cannot connect".
        """
        if self._ae is None:
            return (0, 0)
        live = [assoc for assoc in self._ae.active_associations if assoc.is_acceptor]
        return (len(live), self._ae.maximum_associations)

    def store_session_count(self) -> int:
        """Outbound C-STORE relay associations currently open toward the PACS."""
        with self._store_lock:
            return len(self._store_sessions)

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
            (evt.EVT_ACCEPTED, self._on_accepted),
            (evt.EVT_REJECTED, self._on_rejected),
            (evt.EVT_C_ECHO, self._on_echo),
            (evt.EVT_C_FIND, self._on_find),
            (evt.EVT_C_MOVE, self._on_move),  # implemented in Task 8
            (evt.EVT_C_STORE, self._on_store),
            (evt.EVT_RELEASED, self._on_assoc_end),
            (evt.EVT_ABORTED, self._on_assoc_end),
            (evt.EVT_CONN_CLOSE, self._on_assoc_end),
        ]
        self._ae = ae
        self._server = ae.start_server((ip, port), block=False, evt_handlers=handlers)
        # maximum_associations is pynetdicom's own cap on concurrent inbound
        # associations; it is logged because exceeding it refuses clients
        # above our handlers, where nothing of ours would otherwise see it.
        logger.info(
            "DIMSE face listening on %s:%s (AET: %s, max_assoc=%d, "
            "cfind_timeout=%.0fs store_timeout=%.0fs)",
            ip,
            port,
            self._aet,
            ae.maximum_associations,
            self._cfind_timeout,
            self._store_timeout,
        )

    def stop(self) -> None:
        """Stop the DIMSE face and close idle store sessions.

        ``ThreadedAssociationServer.shutdown()`` closes the listening socket
        but never joins the per-association threads, so a store can still be
        in flight on its own thread when this runs. Idle sessions close right
        away; in-flight ones are doomed instead — same deferral as
        ``_on_assoc_end`` — so close() never races the store it belongs to.
        """
        if self._server is not None:
            live, _ = self.association_load()
            logger.info("DIMSE face stopping (%d association(s) still live)", live)
            started = time.monotonic()
            self._server.shutdown()
            self._server = None
            self._ae = None
            logger.info("DIMSE face stopped in %.1fs", time.monotonic() - started)
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
    def _on_accepted(self, event: evt.Event) -> None:
        peer = _peer(event.assoc)
        with self._assoc_lock:
            self._assoc_stats[event.assoc] = _AssocStats(peer=peer, started=time.monotonic())
        live, maximum = self.association_load()
        logger.info("association accepted from %s (%d/%d slots in use)", peer, live, maximum)
        if maximum and live >= maximum:
            logger.warning(
                "association limit reached (%d/%d) — until a slot frees, every further "
                "request from any client is refused before reaching a handler",
                live,
                maximum,
            )

    def _on_rejected(self, event: evt.Event) -> None:
        """Log the A-ASSOCIATE-RJ that was just sent to a client.

        pynetdicom announces its own rejection with a bare ``Rejecting
        Association`` at INFO, so this is the only record that carries the
        reason. Under load that reason is normally the association limit; the
        other common one is a called AET that does not match ``dimse.aet``.
        """
        primitive = getattr(event.assoc.acceptor, "primitive", None)
        source = getattr(primitive, "result_source", None)
        diagnostic = getattr(primitive, "diagnostic", None)
        requested = getattr(event.assoc.requestor, "primitive", None)
        live, maximum = self.association_load()
        logger.warning(
            "association REJECTED from %s (called AET %r): %s, source %s — %s [%d/%d slots in use]",
            _peer(event.assoc),
            getattr(requested, "called_ae_title", "?"),
            _REJECT_RESULT.get(getattr(primitive, "result", None), "?"),
            _REJECT_SOURCE.get(source, "?"),
            _REJECT_REASON.get((source, diagnostic), "unknown reason"),
            live,
            maximum,
        )

    def _log_assoc_end(self, event: evt.Event) -> None:
        """Summarise an association once, whichever end-event fires first."""
        with self._assoc_lock:
            stats = self._assoc_stats.pop(event.assoc, None)
        if stats is None:
            return  # a later end-event for an association already summarised
        live, maximum = self.association_load()
        logger.info(
            "association %s from %s after %.1fs [%s] (%d/%d slots in use)",
            event.event.name.removeprefix("EVT_").lower(),
            stats.peer,
            time.monotonic() - stats.started,
            stats.summary(),
            live,
            maximum,
        )

    def _bump(self, assoc: Any, key: str) -> None:
        with self._assoc_lock:
            stats = self._assoc_stats.get(assoc)
            if stats is not None:
                stats.bump(key)

    def _finished(self, op: str, started: float, outcome: str, fmt: str, *args: object) -> None:
        """One line per finished DIMSE operation, at WARNING once it ran long.

        A slow operation is the shape of the load problem: it holds both its
        SCP worker thread and its association slot for the whole duration, so a
        handful of them is enough to lock every other client out.
        """
        elapsed = time.monotonic() - started
        level = logging.WARNING if elapsed >= self._slow_seconds else logging.INFO
        logger.log(level, f"{op} {outcome} in %.1fs ({fmt})", elapsed, *args)

    def _on_echo(self, event: evt.Event) -> int:
        self._bump(event.assoc, "echo")
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
        started = time.monotonic()
        try:
            with self._inflight.track("store"):
                ds = event.dataset
                ds.file_meta = event.file_meta
                sop = str(getattr(ds, "SOPInstanceUID", "") or "")
                status = session.store(ds)
            logger.debug(
                "C-STORE relay 0x%04X in %.2fs (sop=%s)",
                status,
                time.monotonic() - started,
                sop or "-",
            )
            if status != 0x0000:
                # The PACS's status relays verbatim to the client, so without
                # this line the client counts failures dicorina never mentions.
                self._warn_once(
                    f"store-status:{status:04X}",
                    "C-STORE relay: PACS answered non-success status 0x%04X (sop=%s peer=%s)",
                    status,
                    sop or "-",
                    _peer(assoc),
                )
            self._bump(assoc, "store_ok" if status == 0x0000 else "store_failed")
            return status
        except NoPresentationContextError as e:
            self._bump(assoc, "store_failed")
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
            self._bump(assoc, "store_failed")
            logger.error(
                "C-STORE relay failed [%s] after %.1fs (sop=%s peer=%s): %s",
                type(e).__name__,
                time.monotonic() - started,
                sop or "-",
                _peer(assoc),
                e,
            )
            return 0xA700  # Out of resources
        except Exception:
            self._bump(assoc, "store_failed")
            logger.exception("C-STORE relay failed (sop=%s peer=%s)", sop or "-", _peer(assoc))
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
        self._log_assoc_end(event)
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
        """First occurrence per key logs WARNING; repeats drop to DEBUG (log-spam guard).

        Callers run on per-association worker threads; the check-and-add is
        locked so a key warns exactly once. Logging stays outside the lock.
        """
        with self._warn_lock:
            first = key not in self._warned_keys
            if first:
                self._warned_keys.add(key)
        if first:
            logger.warning(msg, *args)
        else:
            logger.debug(msg, *args)

    def _on_find(self, event: evt.Event) -> Iterator[tuple[int, Dataset | None]]:
        ident = event.identifier
        # For log context only — the raw identifier passes through to iter_find untouched.
        level = str(getattr(ident, "QueryRetrieveLevel", "") or "")
        study = str(getattr(ident, "StudyInstanceUID", "") or "")
        series = str(getattr(ident, "SeriesInstanceUID", "") or "")
        model = event.context.abstract_syntax  # Patient/Study Root, as negotiated
        peer = _peer(event.assoc)
        self._bump(event.assoc, "find")
        started = time.monotonic()
        matched = 0
        # "incomplete" survives an early generator close (the client hung up or
        # cancelled the query), which the log would otherwise not distinguish
        # from a clean finish.
        outcome = "incomplete"
        logger.debug(
            "C-FIND from %s (level=%s study=%s series=%s)",
            peer,
            level or "-",
            study or "-",
            series or "-",
        )
        with self._inflight.track("find"):
            gen = self._query.iter_find(ident, model=model, timeout=self._cfind_timeout)
            try:
                for ds in gen:
                    if event.is_cancelled:
                        outcome = "cancelled"
                        yield (0xFE00, None)  # upstream released in finally
                        return
                    matched += 1
                    yield (0xFF00, ds)  # same SCP thread, no event loop hop
                outcome = "ok"
            except (PoolExhaustedError, AssociationError) as e:
                outcome = "refused"
                logger.error(
                    "DIMSE C-FIND refused [%s] (peer=%s level=%s study=%s series=%s): %s",
                    type(e).__name__,
                    peer,
                    level or "-",
                    study or "-",
                    series or "-",
                    e,
                )
                yield (0xA700, None)  # Refused: Out of Resources
                return
            except FindFailedError as e:
                outcome = f"upstream 0x{e.status:04X}"
                logger.error(
                    "DIMSE C-FIND upstream failure [%s] (peer=%s level=%s study=%s series=%s): %s",
                    type(e).__name__,
                    peer,
                    level or "-",
                    study or "-",
                    series or "-",
                    e,
                )
                yield (e.status, None)  # transparent PACS status forward
                return
            except Exception as e:
                outcome = "error"
                logger.exception(
                    "DIMSE C-FIND failed [%s] (peer=%s level=%s study=%s series=%s)",
                    type(e).__name__,
                    peer,
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
                self._finished(
                    "C-FIND",
                    started,
                    outcome,
                    "peer=%s level=%s study=%s series=%s matched=%d",
                    peer,
                    level or "-",
                    study or "-",
                    series or "-",
                    matched,
                )
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
        peer = _peer(event.assoc)
        self._bump(event.assoc, "move")
        dest = self._allowlist.resolve(dest_aet)
        if dest is None:
            logger.warning(
                "C-MOVE from %s to unknown destination AET %r refused (level=%s study=%s) — "
                "add it to [dimse.allowlist] if the destination is legitimate",
                peer,
                dest_aet,
                level,
                study or "-",
            )
            yield (None, None)  # → 0xA801 Move Destination unknown
            return
        yield (dest.host, dest.port)

        started = time.monotonic()
        outcome = "incomplete"
        count = 0
        sent = 0
        logger.info(
            "C-MOVE from %s to %s@%s:%d (level=%s study=%s series=%s)",
            peer,
            dest_aet,
            dest.host,
            dest.port,
            level,
            study or "-",
            series or "-",
        )
        with self._inflight.track("move"):
            try:
                # Sub-operation count from series-level C-FIND (never instance-level).
                try:
                    if level == "SERIES" and series:
                        count, iterator = self._series_move(study, series)
                    else:
                        count, iterator = self._study_move(study)
                except Exception as e:
                    outcome = "planning failed"
                    logger.exception(
                        "C-MOVE planning failed [%s] for study=%s", type(e).__name__, study
                    )
                    yield 0
                    yield (0xA702, None)  # Unable to perform sub-operations
                    return
                yield count

                try:
                    for ds in iterator:
                        if event.is_cancelled:
                            outcome = "cancelled"
                            yield (0xFE00, None)
                            return
                        sent += 1
                        yield (0xFF00, ds)
                    outcome = "ok"
                except (
                    MoveToSelfError,
                    ArrivalTimeoutError,
                    AssociationError,
                    PoolExhaustedError,
                ) as e:
                    outcome = f"failed [{type(e).__name__}]"
                    logger.exception(
                        "C-MOVE pass-through failed [%s] for study=%s", type(e).__name__, study
                    )
                    yield (0xA702, None)
                    return
            finally:
                self._finished(
                    "C-MOVE",
                    started,
                    outcome,
                    "peer=%s dest=%s level=%s study=%s series=%s sent=%d/%d",
                    peer,
                    dest_aet,
                    level,
                    study or "-",
                    series or "-",
                    sent,
                    count,
                )

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
