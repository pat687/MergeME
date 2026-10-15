"""Endpoints anyone can call: health checks, form metadata and the intake form itself."""

from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_db, get_settings
from app.config import Settings
from app.db import utcnow
from app.domain import compliance
from app.domain.types import LeadStatus, PracticeArea
from app.schemas import IntakeRequest, IntakeResponse
from app.services import pipeline

router = APIRouter()
log = logging.getLogger(__name__)


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
    name = settings.platform_name
    return {
        "platform_name": name,
        "practice_areas": [{"value": p.value, "label": p.label} for p in PracticeArea],
        "disclosure": compliance.disclosure(name, settings.operator),
        "consent_text": compliance.consent_text(name, settings.shared_max_attorneys),
        "phone_placeholder": compliance.PHONE_PLACEHOLDER,
        "sensitive_consent": {
            area: compliance.sensitive_consent_text(name, area)
            for area in compliance.SENSITIVE_AREAS
        },
        "area_notices": compliance.area_notices(name),
        "injury_areas": sorted(compliance.INJURY_AREAS),
        "max_attorneys": settings.shared_max_attorneys,
        "consent_version": settings.disclosure_version,
        # Which practice areas are open in each state, so the form can say so before
        # anyone types details we couldn't keep.
        "open": {
            state: [p.value for p in PracticeArea if not settings.closed_reason(state, p)]
            for state in sorted(settings.launch_states)
        },
        "responsible_attorneys": settings.responsible_attorneys,
        "privacy_email": settings.privacy_email,
        "operator": settings.operator,
        "retention_days": {
            "rejected": settings.retention_rejected_days,
            "unrouted": settings.retention_unrouted_days,
            "narrative": settings.retention_narrative_days,
            "evidence": settings.retention_evidence_days,
            "sent_email": settings.retention_outbox_days,
        },
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
    disclosure = compliance.disclosure(settings.platform_name, settings.operator)
    if refused := pipeline.refusal(settings, inquiry):
        # Nothing about this person is stored or sent anywhere.
        log.info("inquiry refused (%s): %s %s", refused, inquiry.state, inquiry.practice_area)
        return IntakeResponse(
            lead_id=None,
            status=LeadStatus.UNSERVICEABLE,
            message=compliance.consumer_message(refused, settings.platform_name),
            disclosure=disclosure,
        )
    meta = pipeline.RequestMeta(
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
        gpc=request.headers.get("sec-gpc", "").strip() == "1",
    )
    lead = pipeline.process_intake(db, settings, inquiry, meta, utcnow())
    return IntakeResponse(
        lead_id=lead.id,
        status=lead.status,
        message=compliance.consumer_message(
            lead.status, settings.platform_name, lead.rejection_reasons
        ),
        disclosure=disclosure,
        reload_form=compliance.OUTDATED_FORM in lead.rejection_reasons,
    )
