"""ProxyService: DICOMweb operations over the dimsechord core."""

from __future__ import annotations

import asyncio
import io
import tempfile
from typing import IO, TYPE_CHECKING, Any

import pydicom
from dimsechord import (
    SeriesQuery,
    build_multipart_response,
    convert_datasets_to_dicom_json,
    extract_frames_from_dataset,
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

    async def study_metadata(self, study_uid: str, base_url: str) -> list[dict[str, Any]]:
        series = await self._client.find_series(
            SeriesQuery(study_instance_uid=study_uid),
            self._pacs,
            timeout=self._cfind_timeout,
        )
        series_uids = [s.series_instance_uid for s in series]
        datasets = [ds async for ds in self._engine.stream_study(study_uid, series_uids)]
        return await asyncio.to_thread(convert_datasets_to_dicom_json, datasets, base_url)

    async def series_metadata(
        self, study_uid: str, series_uid: str, base_url: str
    ) -> list[dict[str, Any]]:
        cached = await self._engine.ensure_series(study_uid, series_uid)
        return await asyncio.to_thread(
            convert_datasets_to_dicom_json, list(cached.instances.values()), base_url
        )

    async def _instance_dataset(self, study_uid, series_uid, instance_uid):  # type: ignore[no-untyped-def]
        mem = self._cache.get_series_from_memory(study_uid, series_uid)
        if mem is not None and instance_uid in mem.instances:
            return mem.instances[instance_uid]
        disk = self._cache.read_instance(study_uid, series_uid, instance_uid)
        if disk is not None:
            return disk
        cached = await self._engine.ensure_series(study_uid, series_uid)
        if instance_uid not in cached.instances:
            raise KeyError(instance_uid)
        return cached.instances[instance_uid]

    async def frames(
        self, study_uid: str, series_uid: str, instance_uid: str, frame_numbers: list[int]
    ) -> tuple[bytes, str]:
        ds = await self._instance_dataset(study_uid, series_uid, instance_uid)
        frames = await asyncio.to_thread(extract_frames_from_dataset, ds, frame_numbers)
        return await asyncio.to_thread(build_multipart_response, frames)

    async def instance(self, study_uid: str, series_uid: str, instance_uid: str) -> bytes:
        ds = await self._instance_dataset(study_uid, series_uid, instance_uid)
        buf = io.BytesIO()
        await asyncio.to_thread(pydicom.dcmwrite, buf, ds, enforce_file_format=True)
        return buf.getvalue()

    async def series_zip(self, study_uid: str, series_uid: str) -> IO[bytes]:
        cached = await self._engine.ensure_series(study_uid, series_uid)
        spooled = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)  # noqa: SIM115
        await asyncio.to_thread(self._cache.build_series_zip, cached, spooled)
        spooled.seek(0)
        return spooled
