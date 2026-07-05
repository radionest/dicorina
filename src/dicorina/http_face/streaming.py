"""Chunked DICOM-JSON response helper shared by QIDO and WADO-metadata routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DICOM_JSON = "application/dicom+json"


async def dicom_json_stream_response(result: AsyncIterator[bytes] | bytes) -> Response:
    """Wrap a byte-chunk stream (or a cached full body) into a response.

    The first chunk is awaited eagerly so upstream errors (pool exhausted,
    association failure, PACS failure status) raise BEFORE any byte or header
    is sent and map through the app's exception handlers. After the first
    chunk the status is committed; a mid-stream error breaks the connection,
    leaving the JSON array unterminated — that is the client's signal.
    """
    if isinstance(result, bytes):
        return Response(content=result, media_type=DICOM_JSON)
    first = await anext(result)

    async def body() -> AsyncIterator[bytes]:
        yield first
        async for chunk in result:
            yield chunk

    return StreamingResponse(body(), media_type=DICOM_JSON)
