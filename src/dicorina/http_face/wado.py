"""WADO-RS routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from dicorina.deps import ServiceDep  # noqa: TC001

router = APIRouter()
DICOM_JSON = "application/dicom+json"


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/dicom-web"


def _parse_frames(frames: str) -> list[int]:
    return [int(n) for n in frames.split(",") if n.strip()]


@router.get("/studies/{study_uid}/metadata")
async def study_metadata(
    study_uid: str, request: Request, service: ServiceDep
) -> JSONResponse:
    meta = await service.study_metadata(study_uid, _base_url(request))
    return JSONResponse(content=meta, media_type=DICOM_JSON)


@router.get("/studies/{study_uid}/series/{series_uid}/metadata")
async def series_metadata(
    study_uid: str, series_uid: str, request: Request, service: ServiceDep
) -> JSONResponse:
    meta = await service.series_metadata(study_uid, series_uid, _base_url(request))
    return JSONResponse(content=meta, media_type=DICOM_JSON)


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances/{instance_uid}/frames/{frames}"
)
async def retrieve_frames(
    study_uid: str,
    series_uid: str,
    instance_uid: str,
    frames: str,
    service: ServiceDep,
) -> Response:
    body, content_type = await service.frames(
        study_uid, series_uid, instance_uid, _parse_frames(frames)
    )
    return Response(content=body, media_type=content_type)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{instance_uid}")
async def retrieve_instance(
    study_uid: str, series_uid: str, instance_uid: str, service: ServiceDep
) -> Response:
    data = await service.instance(study_uid, series_uid, instance_uid)
    return Response(content=data, media_type="application/dicom")


@router.get("/studies/{study_uid}/series/{series_uid}/archive")
async def download_series_archive(
    study_uid: str, series_uid: str, service: ServiceDep
) -> StreamingResponse:
    spooled = await service.series_zip(study_uid, series_uid)
    return StreamingResponse(
        spooled,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{series_uid}.zip"'},
    )
