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
from app.domain.matching import covers_zip
from app.domain.types import BillingPlan, DistributionModel, LeadStatus, OfferStatus, PracticeArea
from app.models import Attorney, BidChange, Lead, LedgerEntry, Offer, OutboxMessage, Territory
from app.schemas import (
    AgreementIn,
    AttorneyCreate,
    AttorneyOut,
    AttorneyUpdate,
    AuditOut,
    BidChangeOut,
    BidLinkOut,
    BillingRunResult,
    EraseIn,
    HoldIn,
    LeadDetail,
    LeadSummary,
    LedgerEntryOut,
    LicenseCheckIn,
    MarketArea,
    OfferOut,
    Statement,
    TerritoryCreate,
    TerritoryOut,
)
from app.services import billing, emails, links, market, outbox, privacy

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


def _check_floors(settings: Settings, plan: str, bids: dict[str, int]) -> None:
    """The same lowest price the bid page enforces. A pay-per-lead price below it would
    never get a lead (the line-ups skip it), so refuse it here instead of failing quietly.
    Subscribers' numbers aren't prices, so they're exempt."""
    if plan == BillingPlan.SUBSCRIPTION:
        return
    low = [
        f"{PracticeArea(area).label}: the lowest price accepted is "
        f"${settings.floor_for(area) / 100:,.2f}"
        for area, cents in bids.items()
        if cents < settings.floor_for(area)
    ]
    if low:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(low))


@router.post("/attorneys", response_model=AttorneyOut, status_code=status.HTTP_201_CREATED)
def create_attorney(
    body: AttorneyCreate, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Attorney:
    data = body.model_dump(mode="json")
    _check_floors(settings, data["plan"], data["bids_cents"])
    attorney = Attorney(**data, created_at=utcnow())
    db.add(attorney)
    db.commit()
    return attorney


@router.patch("/attorneys/{attorney_id}", response_model=AttorneyOut)
def update_attorney(
    attorney_id: int,
    body: AttorneyUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Attorney:
    attorney = _get_or_404(db, Attorney, attorney_id)
    changes = body.model_dump(mode="json", exclude_unset=True)
    if "bids_cents" in changes:
        _check_floors(settings, attorney.plan, changes["bids_cents"])
    if changes.get("active") is False and attorney.active:
        # Deactivating cuts off every bid link for good: reactivating won't revive one
        # that leaked. Send a new link after reactivating.
        attorney.bids_link_version += 1
    for field, value in changes.items():
        setattr(attorney, field, value)
    db.commit()
    return attorney


@router.post(
    "/attorneys/{attorney_id}/bid-link",
    response_model=BidLinkOut,
    summary="Email an attorney the link to manage their own bids",
)
def send_bid_link(
    attorney_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> BidLinkOut:
    attorney = _get_or_404(db, Attorney, attorney_id)
    if not attorney.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "this attorney is inactive")
    # A new link turns off every earlier one, so a forwarded or leaked link can be cut off.
    db.refresh(attorney, with_for_update=True)
    attorney.bids_link_version += 1
    link = links.bids_link(
        settings.public_base_url,
        settings.link_signing_secret,
        attorney.id,
        attorney.bids_link_version,
    )
    outbox.enqueue(db, "email", emails.bids_email(settings, attorney, link))
    db.commit()
    return BidLinkOut(sent_to=attorney.email, link=link if settings.demo_mode else None)


@router.post(
    "/attorneys/{attorney_id}/license-check",
    response_model=AttorneyOut,
    summary="Record that staff checked the attorney's bar license(s) on the state bar's site",
)
def record_license_check(
    attorney_id: int, body: LicenseCheckIn, db: Session = Depends(get_db)
) -> Attorney:
    attorney = _get_or_404(db, Attorney, attorney_id)
    missing = sorted(set(attorney.bar_states) - set(body.license_numbers))
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"give a license number for every state the attorney takes leads in: {missing}",
        )
    attorney.license_numbers = body.license_numbers
    attorney.license_verified_at = utcnow()
    db.commit()
    return attorney


@router.post(
    "/attorneys/{attorney_id}/agreement",
    response_model=AttorneyOut,
    summary="Record that the attorney accepted a version of the participation agreement",
)
def record_agreement(
    attorney_id: int, body: AgreementIn, db: Session = Depends(get_db)
) -> Attorney:
    attorney = _get_or_404(db, Attorney, attorney_id)
    attorney.agreement_version = body.version
    attorney.agreement_accepted_at = body.accepted_at or utcnow()
    db.commit()
    return attorney


# --------------------------------------------------------------------------- #
# People's data: access and deletion requests, legal holds, retention
# --------------------------------------------------------------------------- #
@router.get("/privacy/lookup", summary="Everything held about a person, by email or phone")
def privacy_lookup(
    email: str | None = None, phone: str | None = None, db: Session = Depends(get_db)
) -> list[dict]:
    if not email and not phone:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give an email or a phone")
    return [privacy.export(db, lead) for lead in privacy.find(db, email, phone)]


@router.post("/leads/{lead_id}/erase", summary="Erase a lead's personal details")
def erase_lead(
    lead_id: str,
    body: EraseIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    lead = _get_or_404(db, Lead, lead_id)
    try:
        privacy.erase(
            db, settings, lead, body.level, utcnow(), reason=body.reason, notify_attorneys=True
        )
    except privacy.PrivacyError as err:
        db.rollback()
        raise HTTPException(err.status_code, err.message) from err
    db.commit()
    return {
        "lead_id": lead.id,
        "narrative_erased_at": lead.narrative_erased_at,
        "erased_at": lead.erased_at,
        "note": None
        if not (settings.exo_sync_enabled and settings.exo_sync_contact_details)
        else "eXo has a copy with contact details: delete it there by hand",
    }


@router.post("/leads/{lead_id}/legal-hold", summary="Put a lead under a legal hold, or lift it")
def legal_hold(lead_id: str, body: HoldIn, db: Session = Depends(get_db)) -> dict:
    lead = _get_or_404(db, Lead, lead_id)
    privacy.set_hold(db, lead, body.on, body.reason)
    db.commit()
    return {"lead_id": lead.id, "legal_hold": bool(lead.legal_hold)}


@router.post("/retention/run", summary="Run the retention schedule now (the worker also does)")
def run_retention(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    return privacy.purge_due(db, settings, utcnow())


@router.get(
    "/bid-changes",
    response_model=list[BidChangeOut],
    summary="Recent saves on attorneys' bid pages, newest first",
)
def bid_changes(
    attorney_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[BidChange]:
    query = select(BidChange).order_by(BidChange.at.desc(), BidChange.id.desc()).limit(limit)
    if attorney_id is not None:
        query = query.where(BidChange.attorney_id == attorney_id)
    return list(db.scalars(query).all())


@router.get("/market", response_model=list[MarketArea], summary="Bids and recent prices per area")
def market_view(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> list[dict]:
    return market.overview(db, settings, utcnow())


@router.get("/territories", response_model=list[TerritoryOut])
def list_territories(db: Session = Depends(get_db)) -> list[Territory]:
    return list(db.scalars(select(Territory).order_by(Territory.id)).all())


@router.post("/territories", response_model=TerritoryOut, status_code=status.HTTP_201_CREATED)
def create_territory(body: TerritoryCreate, db: Session = Depends(get_db)) -> Territory:
    owner = _get_or_404(db, Attorney, body.attorney_id)
    # Selling a territory the owner could never receive leads in would just be rent
    # for nothing, so refuse it up front.
    if body.practice_area not in owner.bids_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{owner.name} doesn't handle {body.practice_area.label}",
        )
    if not covers_zip(owner.service_zips, body.zip_code):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{owner.name} doesn't cover ZIP {body.zip_code}"
        )
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
