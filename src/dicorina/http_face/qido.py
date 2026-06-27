"""QIDO-RS routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from dicorina.deps import ServiceDep  # noqa: TC001

router = APIRouter()
DICOM_JSON = "application/dicom+json"


@router.get("/studies")
async def search_studies(request: Request, service: ServiceDep) -> JSONResponse:
    results = await service.search_studies(dict(request.query_params))
    return JSONResponse(content=results, media_type=DICOM_JSON)


@router.get("/studies/{study_uid}/series")
async def search_series(study_uid: str, request: Request, service: ServiceDep) -> JSONResponse:
    results = await service.search_series(study_uid, dict(request.query_params))
    return JSONResponse(content=results, media_type=DICOM_JSON)


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def search_instances(
    study_uid: str, series_uid: str, request: Request, service: ServiceDep
) -> JSONResponse:
    results = await service.search_instances(study_uid, series_uid, dict(request.query_params))
    return JSONResponse(content=results, media_type=DICOM_JSON)
