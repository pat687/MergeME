"""Shared request plumbing: database sessions, settings and the admin key check."""

from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


def require_admin(
    x_admin_key: str = Header(default="", description="Admin API key"),
    settings: Settings = Depends(get_settings),
) -> None:
    if not hmac.compare_digest(
        x_admin_key.encode("utf-8", "replace"), settings.admin_api_key.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin key required")
