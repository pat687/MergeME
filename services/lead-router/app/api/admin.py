"""Back-office endpoints for the operations team. All require the X-Admin-Key header."""

from __future__ import annotations

from statistics import mean

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import worker
from app.api.deps import get_db, get_settings, require_admin
from app.config import Settings
from app.db import utcnow
from app.domain.types import DistributionModel, LeadStatus, OfferStatus
from app.models import Attorney, Lead, LedgerEntry, Offer, OutboxMessage, Territory
from app.schemas import (
    AttorneyCreate,
    AttorneyOut,
    AttorneyUpdate,
    AuditOut,
    BillingRunResult,
    LeadDetail,
    LeadSummary,
    LedgerEntryOut,
    OfferOut,
    Statement,
    TerritoryCreate,
    TerritoryOut,
)
from app.services import billing, links

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])

PERIOD = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Billing month, YYYY-MM")


def _get_or_404(db: Session, model, key):
    row = db.get(model, key)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__name__} {key} not found")
    return row


# --------------------------------------------------------------------------- #
# Dashboard numbers
# --------------------------------------------------------------------------- #
@router.get("/stats", summary="Headline numbers for the dashboard")
def stats(db: Session = Depends(get_db)) -> dict:
    period = billing.period_of(utcnow())
    by_status = dict(db.execute(select(Lead.status, func.count()).group_by(Lead.status)).all())
    by_model = dict(
        db.execute(
            select(Lead.distribution_model, func.count())
            .where(Lead.distribution_model.is_not(None))
            .group_by(Lead.distribution_model)
        ).all()
    )
    revenue = db.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.period == period
        )
    )
    claims = db.execute(
        select(Offer.responded_at, Lead.created_at)
        .join(Lead, Lead.id == Offer.lead_id)
        .where(
            Offer.status == OfferStatus.ACCEPTED,
            Lead.distribution_model != DistributionModel.EXCLUSIVE,
        )
        .order_by(Offer.responded_at.desc())
        .limit(500)
    ).all()
    speed = [(answered - created).total_seconds() for answered, created in claims]
    assigned = by_status.get(LeadStatus.ASSIGNED, 0)
    finished = assigned + by_status.get(LeadStatus.UNSOLD, 0)
    return {
        "leads_by_status": by_status,
        "leads_by_model": by_model,
        "total_leads": sum(by_status.values()),
        "sell_through_rate": round(assigned / finished, 3) if finished else None,
        "avg_seconds_to_claim": round(mean(speed), 1) if speed else None,
        "revenue_cents_this_month": revenue,
        "billing_period": period,
        "outbox_pending": db.scalar(
            select(func.count()).where(
                OutboxMessage.sent_at.is_(None), OutboxMessage.failed.is_(False)
            )
        ),
        "outbox_failed": db.scalar(select(func.count()).where(OutboxMessage.failed.is_(True))),
    }


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
@router.get("/leads", response_model=list[LeadSummary], summary="Most recent leads")
def list_leads(
    status_filter: LeadStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Lead]:
    stmt = select(Lead).order_by(Lead.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Lead.status == status_filter)
    return list(db.scalars(stmt).all())


@router.get(
    "/leads/{lead_id}",
    response_model=LeadDetail,
    summary="Everything about one lead, including WHY each decision was made",
)
def lead_detail(
    lead_id: str, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> LeadDetail:
    lead = _get_or_404(db, Lead, lead_id)
    offers_out = []
    for o in lead.offers:
        out = OfferOut.model_validate(o)
        if settings.demo_mode:
            out.link = links.offer_link(
                settings.public_base_url, settings.link_signing_secret, o.id
            )
        offers_out.append(out)
    return LeadDetail(
        **LeadSummary.model_validate(lead).model_dump(),
        first_name=lead.first_name,
        last_name=lead.last_name,
        email=lead.email,
        phone=lead.phone,
        description=lead.description,
        rejection_reasons=lead.rejection_reasons,
        score_breakdown=lead.score_breakdown,
        offers=offers_out,
        audit=[AuditOut.model_validate(a) for a in lead.audit],
    )


# --------------------------------------------------------------------------- #
# Attorneys and territories
# --------------------------------------------------------------------------- #
@router.get("/attorneys", response_model=list[AttorneyOut])
def list_attorneys(db: Session = Depends(get_db)) -> list[Attorney]:
    return list(db.scalars(select(Attorney).order_by(Attorney.id)).all())


@router.post("/attorneys", response_model=AttorneyOut, status_code=status.HTTP_201_CREATED)
def create_attorney(body: AttorneyCreate, db: Session = Depends(get_db)) -> Attorney:
    attorney = Attorney(**body.model_dump(mode="json"), created_at=utcnow())
    db.add(attorney)
    db.commit()
    return attorney


@router.patch("/attorneys/{attorney_id}", response_model=AttorneyOut)
def update_attorney(
    attorney_id: int, body: AttorneyUpdate, db: Session = Depends(get_db)
) -> Attorney:
    attorney = _get_or_404(db, Attorney, attorney_id)
    for field, value in body.model_dump(mode="json", exclude_unset=True).items():
        setattr(attorney, field, value)
    db.commit()
    return attorney


@router.get("/territories", response_model=list[TerritoryOut])
def list_territories(db: Session = Depends(get_db)) -> list[Territory]:
    return list(db.scalars(select(Territory).order_by(Territory.id)).all())


@router.post("/territories", response_model=TerritoryOut, status_code=status.HTTP_201_CREATED)
def create_territory(body: TerritoryCreate, db: Session = Depends(get_db)) -> Territory:
    _get_or_404(db, Attorney, body.attorney_id)
    territory = Territory(**body.model_dump(mode="json"))
    db.add(territory)
    try:
        db.commit()
    except IntegrityError as err:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that ZIP + practice area already has an exclusive owner"
        ) from err
    return territory


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
@router.post(
    "/billing/run",
    response_model=BillingRunResult,
    summary="Post monthly flat fees (safe to re-run)",
)
def run_billing(period: str = PERIOD, db: Session = Depends(get_db)) -> BillingRunResult:
    created, skipped = billing.run_monthly_billing(db, period, utcnow())
    return BillingRunResult(period=period, created=created, skipped_existing=skipped)


@router.get("/attorneys/{attorney_id}/statement", response_model=Statement)
def attorney_statement(
    attorney_id: int, period: str = PERIOD, db: Session = Depends(get_db)
) -> Statement:
    attorney = _get_or_404(db, Attorney, attorney_id)
    entries = billing.statement(db, attorney_id, period)
    return Statement(
        attorney_id=attorney.id,
        attorney_name=attorney.name,
        period=period,
        entries=[LedgerEntryOut.model_validate(e) for e in entries],
        total_cents=sum(e.amount_cents for e in entries),
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
@router.post("/worker/run", summary="Run one worker cycle now (expire offers, send outbox)")
def run_worker_now(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    return worker.run_cycle(request.app.state.session_factory, settings, request.app.state.handlers)
