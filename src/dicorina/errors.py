"""Map dimsechord exceptions to HTTP status codes (§8)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dimsechord import (
    ArrivalTimeoutError,
    AssociationError,
    DimsechordError,
    FindFailedError,
    MoveToSelfError,
    PoolExhaustedError,
)
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

_STATUS = {
    PoolExhaustedError: 503,
    AssociationError: 503,
    ArrivalTimeoutError: 504,
    MoveToSelfError: 502,
    FindFailedError: 502,
    DimsechordError: 502,
}


def register_exception_handlers(app: FastAPI) -> None:
    async def _handle(_request: Request, exc: Exception) -> JSONResponse:
        for exc_type, status in _STATUS.items():
            if isinstance(exc, exc_type):
                headers = {"Retry-After": "1"} if status == 503 else None
                return JSONResponse(
                    status_code=status, content={"error": str(exc)}, headers=headers
                )
        return JSONResponse(status_code=502, content={"error": str(exc)})

    for exc_type in _STATUS:
        app.add_exception_handler(exc_type, _handle)
