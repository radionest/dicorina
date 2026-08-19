"""Runtime load snapshot — the periodic line that explains a silent stall.

dicorina serves DIMSE from a bounded set of pynetdicom association threads and
runs everything else on one event loop plus asyncio's default thread executor.
Each of those is a queue that fills up quietly: when it does, clients see
refused associations or hanging requests while the journal stays silent,
because from the proxy's point of view nothing has failed yet. This module
counts what is in flight and prints it on an interval, escalating to WARNING
once a limit is actually reached.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dicorina.dimse_face.face import DimseFace

logger = logging.getLogger(__name__)

# An event loop that resumes a timer this late is blocked by synchronous work,
# which stalls every HTTP request and every C-MOVE plan at once.
_LAG_WARN_MS = 1000.0


class InflightCounter:
    """Per-operation in-flight counts and ages, from the SCP worker threads.

    The age matters as much as the count: an operation that never returns logs
    nothing when it finishes, because it never finishes — the only trace it
    leaves is sitting in here getting older while it holds an association slot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peak: dict[str, int] = {}
        self._started: dict[str, dict[int, float]] = {}
        self._next_token = 0

    @contextmanager
    def track(self, kind: str) -> Iterator[None]:
        started = time.monotonic()
        with self._lock:
            self._next_token += 1
            token = self._next_token
            live = self._started.setdefault(kind, {})
            live[token] = started
            self._peak[kind] = max(self._peak.get(kind, 0), len(live))
        try:
            yield
        finally:
            with self._lock:
                self._started[kind].pop(token, None)

    def snapshot(self) -> dict[str, tuple[int, int, float]]:
        """``{kind: (in flight now, peak since start, age of the oldest)}``.

        The age is 0 for a kind with nothing in flight.
        """
        now = time.monotonic()
        with self._lock:
            out: dict[str, tuple[int, int, float]] = {}
            for kind, peak in self._peak.items():
                live = self._started.get(kind) or {}
                out[kind] = (len(live), peak, now - min(live.values()) if live else 0.0)
            return out


def executor_load(loop: asyncio.AbstractEventLoop) -> str:
    """``queued/workers`` for asyncio's default thread executor.

    Every ``asyncio.to_thread`` runs there — the QIDO and WADO stream
    producers, zip building, pydicom writes — and each streaming response holds
    a worker for its whole lifetime. A full executor queues further calls
    indefinitely without raising, so a non-zero queue is the signature of that
    stall. The attributes are private, hence the defensive reads.
    """
    executor = getattr(loop, "_default_executor", None)
    if executor is None:
        # Distinct from "0/0": no to_thread call has built one yet, or the loop
        # implementation keeps it elsewhere. Reporting an idle executor here
        # would read as evidence it is idle.
        return "none"
    work_queue = getattr(executor, "_work_queue", None)
    max_workers = getattr(executor, "_max_workers", None)
    if work_queue is None or max_workers is None:
        return "?/?"
    return f"{work_queue.qsize()}/{max_workers}"


class LoadSnapshotLoop:
    """Logs one load line per interval, at WARNING when a limit is reached."""

    def __init__(
        self,
        face: DimseFace,
        *,
        interval_seconds: float = 60.0,
        slow_operation_seconds: float = 10.0,
    ) -> None:
        self._face = face
        self._interval = interval_seconds
        self._slow_seconds = slow_operation_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None and self._interval > 0:
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                due = time.monotonic() + self._interval
                await asyncio.sleep(self._interval)
                try:
                    # Overshoot is event-loop lag: the loop could not resume
                    # this timer on time because something blocked it.
                    self.log_once(lag_ms=(time.monotonic() - due) * 1000)
                except Exception as e:  # never let the loop die (as EvictionLoop)
                    logger.error(f"Load snapshot failed: {e}")
        except asyncio.CancelledError:
            pass

    def log_once(self, *, lag_ms: float = 0.0) -> None:
        live, maximum = self._face.association_load()
        inflight = self._face.inflight.snapshot()
        ops = " ".join(
            f"{k}={now}(peak {peak}, oldest {oldest:.0f}s)"
            if now
            else f"{k}=0(peak {peak})"
            for k, (now, peak, oldest) in sorted(inflight.items())
        )
        stuck = [
            k
            for k, (now, _, oldest) in inflight.items()
            if now and oldest >= self._slow_seconds
        ]
        line = (
            f"load: dimse_assoc={live}/{maximum} "
            f"store_sessions={self._face.store_session_count()} "
            f"ops[{ops or '-'}] threads={threading.active_count()} "
            f"executor={executor_load(asyncio.get_running_loop())} "
            f"loop_lag_ms={lag_ms:.0f}"
        )
        reasons = []
        if maximum and live >= maximum:
            reasons.append("association limit reached — further requests are being refused")
        if stuck:
            reasons.append(
                f"{', '.join(sorted(stuck))} in flight past slow_operation_seconds "
                "— holding association slots"
            )
        if lag_ms > _LAG_WARN_MS:
            reasons.append("event loop blocked")
        if reasons:
            logger.warning("%s — %s", line, "; ".join(reasons))
        else:
            logger.info(line)
