"""Map dimsechord exceptions to HTTP status codes (§8)."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

_STATUS = {
    PoolExhaustedError: 503,
    AssociationError: 503,
    ArrivalTimeoutError: 504,
    MoveToSelfError: 502,
    FindFailedError: 502,
    DimsechordError: 502,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Map core exceptions to status codes — and log every one of them.

    These handlers are the whole HTTP error path: a pool-exhausted 503 or a
    timed-out 504 reaches the client and, without a line here, leaves nothing
    at all behind in the journal.
    """

    async def _handle(request: Request, exc: Exception) -> JSONResponse:
        for exc_type, status in _STATUS.items():
            if isinstance(exc, exc_type):
                # 503 is backpressure the caller is expected to retry, so it
                # warns; anything else is a genuine upstream failure.
                logger.log(
                    logging.WARNING if status == 503 else logging.ERROR,
                    "HTTP %d %s %s [%s]: %s",
                    status,
                    request.method,
                    request.url.path,
                    type(exc).__name__,
                    exc,
                )
                headers = {"Retry-After": "1"} if status == 503 else None
                return JSONResponse(
                    status_code=status, content={"error": str(exc)}, headers=headers
                )
        # exc_info=exc rather than logger.exception: the handler may run
        # outside the except block that raised, where sys.exc_info() is empty.
        logger.error(
            "HTTP 502 %s %s [%s]",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc_info=exc,
        )
        return JSONResponse(status_code=502, content={"error": str(exc)})

    for exc_type in _STATUS:
        app.add_exception_handler(exc_type, _handle)
