from __future__ import annotations

import pytest
from dimsechord import DicomNode

from dicorina.config import HealthcheckConfig
from dicorina.healthcheck import Healthcheck


@pytest.mark.asyncio
async def test_startup_echo_ok_against_fake_pacs(app_client) -> None:
    client, _ = app_client
    resp = await client.get("/health")
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["pacs_echo"] in {"ok", "fail"}


@pytest.mark.asyncio
async def test_echo_fail_when_pacs_dead(free_port) -> None:
    dead = free_port()
    pacs = DicomNode(aet="DEAD", host="127.0.0.1", port=dead)
    hc = Healthcheck(pacs, engine=None, config=HealthcheckConfig(), primary_aet="DICORINA")
    await hc.startup()
    snap = hc.snapshot()
    assert snap["pacs_echo"] == "fail"
    assert snap["status"] == "degraded"
