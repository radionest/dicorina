from __future__ import annotations

import pytest


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
