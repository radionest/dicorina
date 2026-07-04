from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from dicorina.app import create_app
from dicorina.config import DicorinaConfig
from tests.factories import make_instance


@pytest.mark.asyncio
async def test_search_studies_returns_seeded_study(app_client, seeded_study) -> None:
    client, _ = app_client
    resp = await client.get("/dicom-web/studies")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/dicom+json")
    studies = resp.json()
    uids = {s["0020000D"]["Value"][0] for s in studies}
    assert seeded_study["study"][0] in uids


@pytest.mark.asyncio
async def test_search_series_of_study(app_client, seeded_study) -> None:
    client, _ = app_client
    study = seeded_study["study"][0]
    resp = await client.get(f"/dicom-web/studies/{study}/series")
    assert resp.status_code == 200
    series_uids = {s["0020000E"]["Value"][0] for s in resp.json()}
    assert set(seeded_study["series"]) <= series_uids


@pytest.mark.asyncio
async def test_search_instances_of_series(app_client, seeded_study) -> None:
    client, _ = app_client
    study, series = seeded_study["study"][0], seeded_study["series"][0]
    resp = await client.get(f"/dicom-web/studies/{study}/series/{series}/instances")
    assert resp.status_code == 200
    sop_uids = {i["00080018"]["Value"][0] for i in resp.json()}
    assert set(seeded_study[series]) == sop_uids


def _seed_extra_studies(fake_pacs, n: int) -> None:
    for i in range(1, n + 1):
        fake_pacs.add_instance(
            make_instance(f"1.2.901.{i}", f"1.2.901.{i}.1", f"1.2.901.{i}.1.1")
        )


def _live_cfg(fake_pacs, free_port, tmp_path, qido_ttl: float) -> DicorinaConfig:
    pool_aet = "STREAMPOOL"
    scp_port = free_port()
    fake_pacs.register_destination(pool_aet, "127.0.0.1", scp_port)
    return DicorinaConfig.model_validate(
        {
            "pacs": {"host": "127.0.0.1", "port": fake_pacs.port, "aet": fake_pacs.aet},
            "pool": {"members": [{"aet": pool_aet, "port": scp_port}], "per_aet_cap": 1},
            "scp": {"bind_ip": "127.0.0.1"},
            "dimse": {"listen_ip": "127.0.0.1", "listen_port": free_port(), "aet": "STREAMFACE"},
            "http": {"bind_host": "127.0.0.1", "bind_port": free_port()},
            "cache": {"dir": str(tmp_path / "cache"), "qido_ttl_seconds": qido_ttl},
            "timeouts": {"cmove": 60.0, "arrival": 30.0, "completion_grace": 2.0},
            "healthcheck": {"interval_seconds": 9999.0},
        }
    )


@asynccontextmanager
async def _live_server(cfg: DicorinaConfig):
    app = create_app(cfg)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=cfg.http.bind_port, log_level="warning", lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn server did not start in time"
        yield f"http://127.0.0.1:{cfg.http.bind_port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_qido_streams_chunks_before_completion(fake_pacs, free_port, tmp_path) -> None:
    # Real uvicorn server on a socket: httpx.ASGITransport buffers the whole
    # ASGI response before returning it, so progressive delivery can only be
    # observed over an actual TCP connection.
    _seed_extra_studies(fake_pacs, 2)
    fake_pacs.find_response_delay = 0.4
    async with _live_server(_live_cfg(fake_pacs, free_port, tmp_path, qido_ttl=0.0)) as base:
        stamps: list[float] = []
        async with (
            httpx.AsyncClient(base_url=base) as client,
            client.stream("GET", "/dicom-web/studies") as resp,
        ):
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("application/dicom+json")
            async for _chunk in resp.aiter_bytes():
                stamps.append(time.monotonic())
        assert len(stamps) >= 3
        assert stamps[-1] - stamps[0] >= 0.6  # first chunk long before the last


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_qido_mid_stream_failure_truncates_and_poisons_nothing(
    fake_pacs, free_port, tmp_path
) -> None:
    # streaming.py: after the first chunk the 200 is committed; an upstream
    # failure mid-stream must cut the connection, leave the JSON array
    # unterminated, and cache nothing.
    _seed_extra_studies(fake_pacs, 2)  # 3 studies total
    fake_pacs.find_response_delay = 0.2
    fake_pacs.fail_find_with = 0xA700
    fake_pacs.fail_find_after = 2
    async with _live_server(_live_cfg(fake_pacs, free_port, tmp_path, qido_ttl=5.0)) as base:
        chunks: list[bytes] = []
        async with httpx.AsyncClient(base_url=base) as client:
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                async with client.stream("GET", "/dicom-web/studies") as resp:
                    assert resp.status_code == 200
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
            body = b"".join(chunks)
            assert body.startswith(b"[")
            assert not body.endswith(b"]")  # truncated mid-array — the client's signal
            upstream_calls = len(fake_pacs.find_identifiers)
            fake_pacs.fail_find_with = None
            ok = await client.get("/dicom-web/studies")
        assert ok.status_code == 200
        assert len(ok.json()) == 3
        assert len(fake_pacs.find_identifiers) == upstream_calls + 1  # miss: partial not cached


@pytest.mark.asyncio
async def test_qido_limit_and_offset(app_client, fake_pacs) -> None:
    client, _ = app_client
    _seed_extra_studies(fake_pacs, 2)
    full = (await client.get("/dicom-web/studies")).json()
    assert len(full) == 3
    page = (await client.get("/dicom-web/studies?limit=1&offset=1")).json()
    assert page == [full[1]]


@pytest.mark.asyncio
async def test_qido_arbitrary_keys_reach_pacs(app_client, fake_pacs) -> None:
    client, _ = app_client
    resp = await client.get("/dicom-web/studies?InstitutionName=HOSP&00081010=CT01")
    assert resp.status_code == 200
    seen = fake_pacs.find_identifiers[-1]
    assert str(seen.InstitutionName) == "HOSP"
    assert str(seen.StationName) == "CT01"


@pytest.mark.asyncio
async def test_qido_includefield_forwarded(app_client, fake_pacs) -> None:
    client, _ = app_client
    resp = await client.get("/dicom-web/studies?includefield=OtherPatientNames")
    assert resp.status_code == 200
    assert "OtherPatientNames" in fake_pacs.find_identifiers[-1]


@pytest.mark.asyncio
async def test_qido_unknown_param_rejected(app_client) -> None:
    client, _ = app_client
    resp = await client.get("/dicom-web/studies?NotADicomTag=1")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_qido_upstream_failure_maps_to_502(app_client, fake_pacs) -> None:
    client, _ = app_client
    fake_pacs.fail_find_with = 0xA700
    resp = await client.get("/dicom-web/studies")
    assert resp.status_code == 502


@pytest.mark.parametrize("app_client", [{"cache": {"qido_ttl_seconds": 5.0}}], indirect=True)
@pytest.mark.asyncio
async def test_qido_cache_hit_is_byte_identical(app_client, fake_pacs) -> None:
    client, _ = app_client
    first = await client.get("/dicom-web/studies")
    upstream_calls = len(fake_pacs.find_identifiers)
    second = await client.get("/dicom-web/studies")
    assert second.content == first.content
    assert len(fake_pacs.find_identifiers) == upstream_calls  # served from cache


@pytest.mark.asyncio
async def test_qido_numeric_vr_filter_roundtrip(app_client, fake_pacs, seeded_study) -> None:
    client, _ = app_client
    study, series = seeded_study["study"][0], seeded_study["series"][0]
    resp = await client.get(f"/dicom-web/studies/{study}/series/{series}/instances?Rows=512")
    assert resp.status_code == 200  # was 500 before the VR coercion fix
    assert int(fake_pacs.find_identifiers[-1].Rows) == 512


@pytest.mark.asyncio
async def test_qido_invalid_numeric_value_is_400(app_client) -> None:
    client, _ = app_client
    resp = await client.get("/dicom-web/studies?Rows=abc")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_qido_disconnect_releases_upstream(app_client, fake_pacs) -> None:
    client, _ = app_client
    _seed_extra_studies(fake_pacs, 5)
    fake_pacs.find_response_delay = 0.3
    async with client.stream("GET", "/dicom-web/studies") as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_bytes():
            break  # client walks away mid-stream
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and fake_pacs.active_associations > 0:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    assert fake_pacs.active_associations == 0


@pytest.mark.asyncio
async def test_qido_find_uses_pool_identity(app_client, fake_pacs) -> None:
    client, ctx = app_client
    await client.get("/dicom-web/studies")
    assert fake_pacs.find_calling_aets[-1] == ctx["pool_aet"]


@pytest.mark.parametrize(
    "app_client",
    [{"pool": {"per_aet_find_cap": 1}, "timeouts": {"find_lease": 0.3}}],
    indirect=True,
)
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_qido_find_cap_exhaustion_returns_503(app_client, fake_pacs) -> None:
    client, _ = app_client
    _seed_extra_studies(fake_pacs, 3)  # 4 studies x 0.3s delay ≈ 1.2s busy window
    fake_pacs.find_response_delay = 0.3
    slow = asyncio.create_task(client.get("/dicom-web/studies"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not fake_pacs.find_calling_aets:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    assert fake_pacs.find_calling_aets, "first find never reached the PACS"
    resp = await client.get("/dicom-web/studies?PatientID=X")
    assert resp.status_code == 503  # PoolExhaustedError after find_lease=0.3s
    assert resp.headers.get("retry-after") == "1"
    first = await slow
    assert first.status_code == 200
