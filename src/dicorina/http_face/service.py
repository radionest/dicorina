"""ProxyService: DICOMweb operations over the dimsechord core."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
from typing import IO, TYPE_CHECKING, Any

import pydicom
from dimsechord import (
    SeriesQuery,
    build_multipart_response,
    dataset_to_dicom_json,
    dataset_to_qido_json,
    extract_frames_from_dataset,
    iter_to_aiter,
)
from pynetdicom.sop_class import (  # type: ignore[attr-defined]
    StudyRootQueryRetrieveInformationModelFind,
)

from dicorina.http_face.params import build_identifier, pagination

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from dimsechord import DicomCache, DicomClient, DicomNode, PullEngine, QueryEngine
    from pydicom import Dataset

    from dicorina.http_face.qido_cache import QidoResultCache


class ProxyService:
    def __init__(
        self,
        client: DicomClient,
        engine: PullEngine,
        cache: DicomCache,
        pacs: DicomNode,
        qido_cache: QidoResultCache,
        query: QueryEngine,
        *,
        cfind_timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._engine = engine
        self._cache = cache
        self._pacs = pacs
        self._qido = qido_cache
        self._query = query
        self._cfind_timeout = cfind_timeout

    def search_studies(
        self, params: dict[str, str], includefields: list[str]
    ) -> AsyncIterator[bytes] | bytes:
        return self._search("STUDY", "STUDY", params, includefields)

    def search_series(
        self, study_uid: str, params: dict[str, str], includefields: list[str]
    ) -> AsyncIterator[bytes] | bytes:
        return self._search(
            "SERIES", f"SERIES:{study_uid}", params, includefields, study_uid=study_uid
        )

    def search_instances(
        self,
        study_uid: str,
        series_uid: str,
        params: dict[str, str],
        includefields: list[str],
    ) -> AsyncIterator[bytes] | bytes:
        return self._search(
            "IMAGE",
            f"IMAGE:{study_uid}/{series_uid}",
            params,
            includefields,
            study_uid=study_uid,
            series_uid=series_uid,
        )

    def _search(
        self,
        level: str,
        scope: str,
        params: dict[str, str],
        includefields: list[str],
        *,
        study_uid: str | None = None,
        series_uid: str | None = None,
    ) -> AsyncIterator[bytes] | bytes:
        key = self._qido.key(scope, params, includefields)
        hit = self._qido.get(key)
        if hit is not None:
            return hit
        identifier = build_identifier(
            level, params, includefields, study_uid=study_uid, series_uid=series_uid
        )
        limit, offset = pagination(params)
        return self._qido_stream(identifier, key, limit, offset)

    def _qido_stream(
        self, identifier: Dataset, cache_key: str, limit: int | None, offset: int
    ) -> AsyncIterator[bytes]:
        def chunks() -> Iterator[bytes]:
            # Producer thread: C-FIND iteration, Dataset→dict conversion and
            # json.dumps all run here, off the event loop; the loop only
            # writes ready bytes.
            gen = self._query.iter_find(
                identifier,
                model=StudyRootQueryRetrieveInformationModelFind,
                timeout=self._cfind_timeout,
            )
            emitted = 0
            first = True
            try:
                for i, ds in enumerate(gen):
                    if i < offset:
                        continue
                    if limit is not None and emitted >= limit:
                        break
                    payload = json.dumps(
                        dataset_to_qido_json(ds), separators=(",", ":"), ensure_ascii=False
                    ).encode()
                    yield (b"[" if first else b",") + payload
                    first = False
                    emitted += 1
            finally:
                # break/close → upstream abort + lease release
                gen.close()  # type: ignore[attr-defined]
            yield b"]" if not first else b"[]"

        return self._tee_into_cache(chunks, cache_key)

    async def _tee_into_cache(
        self, make_chunks: Any, cache_key: str
    ) -> AsyncIterator[bytes]:
        buf: list[bytes] = []
        async for chunk in iter_to_aiter(make_chunks):
            buf.append(chunk)
            yield chunk
        # Reached only on a complete, error-free stream (']' emitted).
        self._qido.put(cache_key, b"".join(buf))

    async def study_metadata(self, study_uid: str, base_url: str) -> AsyncIterator[bytes]:
        series = await self._client.find_series(
            SeriesQuery(study_instance_uid=study_uid),
            self._pacs,
            timeout=self._cfind_timeout,
        )
        series_uids = [s.series_instance_uid for s in series]
        return self._metadata_stream(
            lambda: self._engine.iter_study(study_uid, series_uids), base_url
        )

    async def series_metadata(
        self, study_uid: str, series_uid: str, base_url: str
    ) -> AsyncIterator[bytes]:
        return self._metadata_stream(
            lambda: self._engine.iter_series(study_uid, series_uid), base_url
        )

    def _metadata_stream(self, make_iter: Any, base_url: str) -> AsyncIterator[bytes]:
        def chunks() -> Iterator[bytes]:
            # Conversion overlaps C-STORE arrival in this producer thread
            # instead of collect-then-convert.
            first = True
            for ds in make_iter():
                payload = json.dumps(
                    dataset_to_dicom_json(ds, base_url),
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                yield (b"[" if first else b",") + payload
                first = False
            yield b"]" if not first else b"[]"

        async def stream() -> AsyncIterator[bytes]:
            async for chunk in iter_to_aiter(chunks):
                yield chunk

        return stream()

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
