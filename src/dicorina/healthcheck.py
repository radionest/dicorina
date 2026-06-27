"""Startup C-ECHO + optional periodic move-to-self self-test (§8)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pynetdicom import AE
from pynetdicom.sop_class import Verification  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from dimsechord import DicomNode, PullEngine

    from dicorina.config import HealthcheckConfig

logger = logging.getLogger(__name__)


class Healthcheck:
    def __init__(
        self,
        pacs: DicomNode,
        engine: PullEngine | None,
        config: HealthcheckConfig,
        *,
        primary_aet: str,
    ) -> None:
        self._pacs = pacs
        self._engine = engine
        self._config = config
        self._primary_aet = primary_aet
        self._pacs_echo = "unknown"
        self._self_test = "skipped"
        self._task: asyncio.Task[None] | None = None

    def _echo_sync(self) -> bool:
        ae = AE(ae_title=self._primary_aet)
        ae.add_requested_context(Verification)
        assoc = ae.associate(self._pacs.host, self._pacs.port, ae_title=self._pacs.aet)
        if not assoc.is_established:
            return False
        try:
            status = assoc.send_c_echo()
            return bool(status) and status.Status == 0x0000
        finally:
            assoc.release()

    async def startup(self) -> None:
        try:
            ok = await asyncio.to_thread(self._echo_sync)
        except Exception as e:
            logger.error(f"Startup C-ECHO error: {e}")
            ok = False
        self._pacs_echo = "ok" if ok else "fail"
        if not ok:
            logger.error("Startup C-ECHO to PACS failed — proxy is degraded")

    async def _run_self_test(self) -> None:
        if self._engine is None or not self._config.test_study_uid:
            return
        try:
            cached = await self._engine.ensure_series(
                self._config.test_study_uid, self._config.test_series_uid
            )
            self._self_test = "ok" if cached.instances else "fail"
        except Exception as e:
            logger.error(f"Move-to-self self-test failed: {e}")
            self._self_test = "fail"

    def start(self) -> None:
        if self._task is None and self._config.test_study_uid:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                await self._run_self_test()
                await asyncio.sleep(self._config.interval_seconds)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def snapshot(self) -> dict[str, Any]:
        degraded = self._pacs_echo == "fail" or self._self_test == "fail"
        return {
            "status": "degraded" if degraded else "ok",
            "pacs_echo": self._pacs_echo,
            "self_test": self._self_test,
        }
