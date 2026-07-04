from __future__ import annotations

import io
import zipfile

import pytest


@pytest.mark.asyncio
async def test_series_metadata_then_frames(app_client, seeded_study) -> None:
    client, _ = app_client
    study, series = seeded_study["study"][0], seeded_study["series"][0]

    meta = await client.get(f"/dicom-web/studies/{study}/series/{series}/metadata")
    assert meta.status_code == 200
    assert meta.headers["content-type"].startswith("application/dicom+json")
    assert len(meta.json()) == len(seeded_study[series])

    sop = seeded_study[series][0]
    frames = await client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/1"
    )
    assert frames.status_code == 200
    assert frames.headers["content-type"].startswith("multipart/related")
    assert len(frames.content) > 0


@pytest.mark.asyncio
async def test_study_metadata_covers_all_series(app_client, seeded_study) -> None:
    client, _ = app_client
    study = seeded_study["study"][0]
    resp = await client.get(f"/dicom-web/studies/{study}/metadata")
    assert resp.status_code == 200
    total = sum(len(seeded_study[s]) for s in seeded_study["series"])
    assert len(resp.json()) == total


@pytest.mark.asyncio
async def test_instance_retrieve_is_dicom(app_client, seeded_study) -> None:
    client, _ = app_client
    study, series = seeded_study["study"][0], seeded_study["series"][0]
    sop = seeded_study[series][0]
    resp = await client.get(f"/dicom-web/studies/{study}/series/{series}/instances/{sop}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/dicom")
    assert resp.content[128:132] == b"DICM"


@pytest.mark.asyncio
async def test_series_archive_zip(app_client, seeded_study) -> None:
    client, _ = app_client
    study, series = seeded_study["study"][0], seeded_study["series"][0]
    resp = await client.get(f"/dicom-web/studies/{study}/series/{series}/archive")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert len(zf.namelist()) == len(seeded_study[series])


@pytest.mark.asyncio
async def test_cross_face_memory_hit(app_client, seeded_study) -> None:
    """Second metadata call is served from the memory tier (no second upstream pull)."""
    client, ctx = app_client
    study, series = seeded_study["study"][0], seeded_study["series"][0]
    await client.get(f"/dicom-web/studies/{study}/series/{series}/metadata")
    cache = ctx["app"].state.cache
    assert cache.get_series_from_memory(study, series) is not None


@pytest.mark.asyncio
async def test_study_metadata_streams_chunked(app_client, seeded_study) -> None:
    client, _ = app_client
    study = seeded_study["study"][0]
    resp = await client.get(f"/dicom-web/studies/{study}/metadata")
    assert resp.status_code == 200
    assert "content-length" not in resp.headers  # chunked StreamingResponse
    meta = resp.json()
    expected = {sop for s in seeded_study["series"] for sop in seeded_study[s]}
    assert {m["00080018"]["Value"][0] for m in meta} == expected
    assert all("BulkDataURI" in m["7FE00010"] for m in meta)
