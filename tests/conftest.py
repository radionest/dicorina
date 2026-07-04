from __future__ import annotations

import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import httpx
import pytest
from pydicom.uid import generate_uid

from dicorina.app import create_app
from dicorina.config import DicorinaConfig
from tests.factories import make_instance
from tests.fake_pacs import FakePacs


@pytest.fixture
def free_port() -> Callable[[], int]:
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    return _free_port


@pytest.fixture
def seeded_study() -> dict[str, list[str]]:
    study = generate_uid()
    s1, s2 = generate_uid(), generate_uid()
    return {
        "study": [study],
        "series": [s1, s2],
        s1: [generate_uid(), generate_uid()],
        s2: [generate_uid(), generate_uid()],
    }


@pytest.fixture
def fake_pacs(
    free_port: Callable[[], int], seeded_study: dict[str, list[str]]
) -> Iterator[FakePacs]:
    pacs = FakePacs(aet="FAKEPACS")
    study = seeded_study["study"][0]
    for series in seeded_study["series"]:
        for sop in seeded_study[series]:
            pacs.add_instance(make_instance(study, series, sop))
    port = free_port()
    pacs.start(port)
    pacs.port = port  # type: ignore[attr-defined]
    try:
        yield pacs
    finally:
        pacs.stop()


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@pytest.fixture
async def app_client(request, fake_pacs, free_port, tmp_path):
    """Live ASGI app wired to the fake PACS; pool AET registered as move dest.

    Override any config subtree via indirect parametrization:
    @pytest.mark.parametrize("app_client", [{"cache": {"qido_ttl_seconds": 5.0}}],
                             indirect=True)
    """
    pool_aet = "DICORINATEST"
    face_aet = "DICORINAFACE"
    scp_port = free_port()
    fake_pacs.register_destination(pool_aet, "127.0.0.1", scp_port)
    base = {
        "pacs": {"host": "127.0.0.1", "port": fake_pacs.port, "aet": fake_pacs.aet},
        "pool": {"members": [{"aet": pool_aet, "port": scp_port}], "per_aet_cap": 1},
        "scp": {"bind_ip": "127.0.0.1"},
        "dimse": {"listen_ip": "127.0.0.1", "listen_port": free_port(), "aet": face_aet},
        "http": {"bind_host": "127.0.0.1", "bind_port": free_port()},
        "cache": {"dir": str(tmp_path / "cache"), "qido_ttl_seconds": 0.0},
        "timeouts": {"cmove": 60.0, "arrival": 30.0, "completion_grace": 2.0},
        "healthcheck": {"interval_seconds": 9999.0},
    }
    cfg = DicorinaConfig.model_validate(_deep_merge(base, getattr(request, "param", {})))
    app = create_app(cfg)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client, {"app": app, "pool_aet": pool_aet, "face_aet": face_aet, "cfg": cfg}
