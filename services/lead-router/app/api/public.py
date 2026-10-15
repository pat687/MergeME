"""Endpoints anyone can call: health checks, form metadata and the intake form itself."""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_db, get_settings
from app.config import Settings
from app.db import utcnow
from app.domain import compliance
from app.domain.types import PracticeArea
from app.schemas import IntakeRequest, IntakeResponse
from app.services import pipeline

router = APIRouter()


def client_ip(request: Request) -> str | None:
    """The caller's address, only if it's a real IP (it's stored as consent evidence).

    X-Forwarded-For is honored only from trusted proxies (uvicorn's
    FORWARDED_ALLOW_IPS, default 127.0.0.1), so a client can't choose this value.
    """
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


@router.get("/healthz", tags=["ops"], summary="Is the process alive?")
def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/readyz", tags=["ops"], summary="Can the process reach its database?")
def readyz(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/api/v1/meta", tags=["intake"], summary="Everything the intake form needs to render")
def meta(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "platform_name": settings.platform_name,
        "practice_areas": [{"value": p.value, "label": p.label} for p in PracticeArea],
        "disclosure": compliance.disclosure(settings.platform_name),
        "consent_text": compliance.consent_text(
            settings.platform_name, settings.shared_max_attorneys
        ),
        "consent_version": settings.disclosure_version,
    }


@router.post(
    "/api/v1/intake",
    tags=["intake"],
    status_code=status.HTTP_201_CREATED,
    response_model=IntakeResponse,
    summary="Submit a legal inquiry",
)
def intake(
    inquiry: IntakeRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IntakeResponse:
    ip = client_ip(request)
    if not request.app.state.intake_limiter.allow(ip or "unknown"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many submissions from your network. Please wait a minute and try again.",
        )
    meta = pipeline.RequestMeta(ip_address=ip, user_agent=request.headers.get("user-agent"))
    lead = pipeline.process_intake(db, settings, inquiry, meta, utcnow())
    return IntakeResponse(
        lead_id=lead.id,
        status=lead.status,
        message=compliance.consumer_message(
            lead.status, settings.platform_name, lead.rejection_reasons
        ),
        disclosure=compliance.disclosure(settings.platform_name),
        reload_form=compliance.OUTDATED_FORM in lead.rejection_reasons,
    )
