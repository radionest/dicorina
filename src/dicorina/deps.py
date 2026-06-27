"""FastAPI dependencies."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request

if TYPE_CHECKING:
    from dicorina.config import DicorinaConfig
    from dicorina.http_face.service import ProxyService


def get_config(request: Request) -> DicorinaConfig:
    return request.app.state.config


def get_service(request: Request) -> ProxyService:
    return request.app.state.service


def verify_token(request: Request) -> None:
    """No-op when http.auth_token is empty (MVP); else require a matching bearer/header."""
    token = request.app.state.config.http.auth_token
    if not token:
        return
    auth = request.headers.get("Authorization", "")
    provided = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    if not provided:
        provided = request.headers.get("X-Internal-Token", "")
    if not (provided and hmac.compare_digest(provided, token)):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


ConfigDep = Annotated["DicorinaConfig", Depends(get_config)]
ServiceDep = Annotated["ProxyService", Depends(get_service)]
