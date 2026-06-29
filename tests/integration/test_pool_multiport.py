from __future__ import annotations

import socket

import httpx
import pytest

from dicorina.app import create_app
from dicorina.config import DicorinaConfig


@pytest.mark.asyncio
async def test_two_members_bind_both_ports_and_retrieve(
    fake_pacs, seeded_study, free_port, tmp_path
) -> None:
    """Two pool AETs, each on its own port: both listeners bind and retrieval works."""
    aets = ["DICORINA1", "DICORINA2"]
    ports = {a: free_port() for a in aets}
    for a in aets:
        fake_pacs.register_destination(a, "127.0.0.1", ports[a])

    cfg = DicorinaConfig.model_validate(
        {
            "pacs": {"host": "127.0.0.1", "port": fake_pacs.port, "aet": fake_pacs.aet},
            "pool": {
                "members": [{"aet": a, "port": ports[a]} for a in aets],
                "per_aet_cap": 1,
            },
            "scp": {"bind_ip": "127.0.0.1"},
            "dimse": {"listen_ip": "127.0.0.1", "listen_port": free_port()},
            "http": {"bind_host": "127.0.0.1", "bind_port": free_port()},
            "cache": {"dir": str(tmp_path / "cache"), "qido_ttl_seconds": 0.0},
            "timeouts": {"cmove": 60.0, "arrival": 30.0, "completion_grace": 2.0},
            "healthcheck": {"interval_seconds": 9999.0},
        }
    )
    app = create_app(cfg)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        # Each per-AET port has its own bound, accepting listener.
        for a in aets:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                assert s.connect_ex(("127.0.0.1", ports[a])) == 0, f"{a} port not bound"

        # Retrieve both series through the proxy: leases round-robin the pool, so the
        # C-MOVE-to-self for each leased AET must land on that AET's bound port.
        study = seeded_study["study"][0]
        for series in seeded_study["series"]:
            resp = await client.get(
                f"/dicom-web/studies/{study}/series/{series}/metadata"
            )
            assert resp.status_code == 200
            assert len(resp.json()) == len(seeded_study[series])
