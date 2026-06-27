"""ProxyService: DICOMweb operations over the dimsechord core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dimsechord import (
    image_result_to_dicom_json,
    series_result_to_dicom_json,
    study_result_to_dicom_json,
)

from dicorina.http_face.params import (
    image_query_from_params,
    series_query_from_params,
    study_query_from_params,
)

if TYPE_CHECKING:
    from dimsechord import DicomCache, DicomClient, DicomNode, PullEngine

    from dicorina.http_face.qido_cache import QidoResultCache


class ProxyService:
    def __init__(
        self,
        client: DicomClient,
        engine: PullEngine,
        cache: DicomCache,
        pacs: DicomNode,
        qido_cache: QidoResultCache,
        *,
        cfind_timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._engine = engine
        self._cache = cache
        self._pacs = pacs
        self._qido = qido_cache
        self._cfind_timeout = cfind_timeout

    async def search_studies(self, params: dict[str, str]) -> list[dict[str, Any]]:
        key = self._qido.key("STUDY", params)
        hit = self._qido.get(key)
        if hit is not None:
            return hit
        results = await self._client.find_studies(
            study_query_from_params(params), self._pacs, timeout=self._cfind_timeout
        )
        out = [study_result_to_dicom_json(r) for r in results]
        self._qido.put(key, out)
        return out

    async def search_series(self, study_uid: str, params: dict[str, str]) -> list[dict[str, Any]]:
        key = self._qido.key(f"SERIES:{study_uid}", params)
        hit = self._qido.get(key)
        if hit is not None:
            return hit
        results = await self._client.find_series(
            series_query_from_params(study_uid, params),
            self._pacs,
            timeout=self._cfind_timeout,
        )
        out = [series_result_to_dicom_json(r) for r in results]
        self._qido.put(key, out)
        return out

    async def search_instances(
        self, study_uid: str, series_uid: str, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        key = self._qido.key(f"IMAGE:{study_uid}/{series_uid}", params)
        hit = self._qido.get(key)
        if hit is not None:
            return hit
        results = await self._client.find_images(
            image_query_from_params(study_uid, series_uid, params),
            self._pacs,
            timeout=self._cfind_timeout,
        )
        out = [image_result_to_dicom_json(r) for r in results]
        self._qido.put(key, out)
        return out
