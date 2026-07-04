"""QIDO-RS routes (chunked streaming)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response  # noqa: TC002

from dicorina.deps import ServiceDep  # noqa: TC001
from dicorina.http_face.streaming import dicom_json_stream_response

router = APIRouter()


def _plain_params(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.query_params.items() if k != "includefield"}


def _includefields(request: Request) -> list[str]:
    return request.query_params.getlist("includefield")


@router.get("/studies")
async def search_studies(request: Request, service: ServiceDep) -> Response:
    result = service.search_studies(_plain_params(request), _includefields(request))
    return await dicom_json_stream_response(result)


@router.get("/studies/{study_uid}/series")
async def search_series(study_uid: str, request: Request, service: ServiceDep) -> Response:
    result = service.search_series(study_uid, _plain_params(request), _includefields(request))
    return await dicom_json_stream_response(result)


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def search_instances(
    study_uid: str, series_uid: str, request: Request, service: ServiceDep
) -> Response:
    result = service.search_instances(
        study_uid, series_uid, _plain_params(request), _includefields(request)
    )
    return await dicom_json_stream_response(result)
