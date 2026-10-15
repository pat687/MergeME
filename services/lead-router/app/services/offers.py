"""Offers: asking attorneys "do you want this lead?" and handling their answers.

The life of an offer:

    queued ──► pending ──► accepted   (attorney said yes: lead is theirs)
                  │
                  ├──────► declined   (attorney said no)
                  ├──────► expired    (attorney didn't answer in time)
                  └──────► lost       (someone else said yes first)

The tricky part is two attorneys clicking "accept" at the same moment in the
shared model. We handle it with compare-and-set: "assign this lead to me ONLY
IF it has no owner yet" is a single database statement, so exactly one of
them wins, even with many servers.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.types import DistributionModel, LeadStatus, OfferStatus
from app.models import Attorney, Lead, Offer
from app.schemas import OfferView
from app.services import audit, billing, emails, links, outbox


class OfferError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def ttl_for(settings: Settings, model: str) -> timedelta:
    seconds = (
        settings.ping_offer_ttl_seconds
        if model == DistributionModel.PING_TREE
        else settings.shared_offer_ttl_seconds
    )
    return timedelta(seconds=seconds)


def start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def accepted_today(db: Session, now: datetime) -> dict[int, int]:
    rows = db.execute(
        select(Offer.attorney_id, func.count())
        .where(Offer.status == OfferStatus.ACCEPTED, Offer.responded_at >= start_of_day(now))
        .group_by(Offer.attorney_id)
    ).all()
    return {attorney_id: count for attorney_id, count in rows}


# --------------------------------------------------------------------------- #
# Side effects (all go through the outbox)
# --------------------------------------------------------------------------- #
def notify_offer(db: Session, settings: Settings, lead: Lead, offer: Offer) -> None:
    link = links.offer_link(settings.public_base_url, settings.link_signing_secret, offer.id)
    attorney = db.get(Attorney, offer.attorney_id)
    outbox.enqueue(db, "email", emails.offer_email(settings, lead, offer, attorney, link))


def finalize_assignment(
    db: Session, settings: Settings, lead: Lead, offer: Offer, now: datetime
) -> None:
    """Everything that happens once a lead belongs to an attorney."""
    attorney = db.get(Attorney, offer.attorney_id)
    billing.charge_for_delivery(db, offer, lead, now)
    outbox.enqueue(db, "email", emails.delivered_email(settings, lead, offer, attorney))
    if settings.exo_sync_enabled and attorney.exo_username:
        outbox.enqueue(
            db, "exo.assign_lead", {"email": lead.email, "assignee": attorney.exo_username}
        )
    audit.record(
        db,
        lead.id,
        "assigned",
        attorney_id=attorney.id,
        attorney=attorney.name,
        model=lead.distribution_model,
        price_cents=offer.price_cents,
    )


# --------------------------------------------------------------------------- #
# Creating offers
# --------------------------------------------------------------------------- #
def activate(db: Session, settings: Settings, lead: Lead, offer: Offer, now: datetime) -> None:
    offer.status = OfferStatus.PENDING
    offer.activated_at = now
    offer.expires_at = now + ttl_for(settings, lead.distribution_model)
    db.flush()
    notify_offer(db, settings, lead, offer)


def advance(db: Session, settings: Settings, lead: Lead, now: datetime) -> None:
    """After a decline or timeout: ask the next attorney, or give up if no one is left."""
    if lead.status != LeadStatus.OFFERED:
        return
    still_waiting = db.scalar(
        select(func.count()).where(Offer.lead_id == lead.id, Offer.status == OfferStatus.PENDING)
    )
    if still_waiting:
        return

    if lead.distribution_model == DistributionModel.PING_TREE:
        counts = accepted_today(db, now)
        queued = db.scalars(
            select(Offer)
            .where(Offer.lead_id == lead.id, Offer.status == OfferStatus.QUEUED)
            .order_by(Offer.round)
        ).all()
        for nxt in queued:
            attorney = db.get(Attorney, nxt.attorney_id)
            # Things may have changed since the line-up was made.
            if not attorney.active or counts.get(attorney.id, 0) >= attorney.daily_cap:
                nxt.status = OfferStatus.SKIPPED
                nxt.responded_at = now
                audit.record(db, lead.id, "offer_skipped", attorney=attorney.name, round=nxt.round)
                continue
            activate(db, settings, lead, nxt, now)
            audit.record(db, lead.id, "offer_activated", attorney=attorney.name, round=nxt.round)
            return

    lead.status = LeadStatus.UNSOLD
    audit.record(db, lead.id, "unsold", reason="every attorney passed or ran out of time")


# --------------------------------------------------------------------------- #
# Attorney actions
# --------------------------------------------------------------------------- #
def _load(db: Session, settings: Settings, offer_id: str, token: str) -> Offer:
    offer = db.get(Offer, offer_id)
    # Same answer for "doesn't exist" and "bad link" so links can't be probed.
    if offer is None or not links.verify_offer(settings.link_signing_secret, offer_id, token):
        raise OfferError(404, "offer not found")
    return offer


def _lock_lead(db: Session, lead_id: str) -> Lead:
    """Lock the lead row before touching any of its offers.

    Every writer (accept, decline, the expiry worker) takes locks in the same
    order, lead first, then offers, so two of them can never deadlock waiting
    on each other. On SQLite this is a no-op because writes are already serial.
    """
    return db.scalars(
        select(Lead)
        .where(Lead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _closed(offer: Offer) -> OfferError:
    if offer.status == OfferStatus.LOST:
        return OfferError(409, "another attorney claimed this lead first")
    if offer.status == OfferStatus.QUEUED:
        return OfferError(409, "this offer is queued; it isn't your turn yet")
    return OfferError(409, f"this offer is already {offer.status}")


def _expire_if_due(db: Session, settings: Settings, offer: Offer, now: datetime) -> bool:
    if offer.status != OfferStatus.PENDING or offer.expires_at is None or offer.expires_at > now:
        return False
    lead = _lock_lead(db, offer.lead_id)
    changed = db.execute(
        update(Offer)
        .where(Offer.id == offer.id, Offer.status == OfferStatus.PENDING)
        .values(status=OfferStatus.EXPIRED, responded_at=now)
    ).rowcount
    if changed:
        db.refresh(offer)
        audit.record(db, lead.id, "offer_expired", attorney_id=offer.attorney_id, round=offer.round)
        advance(db, settings, lead, now)
    return bool(changed)


def accept(db: Session, settings: Settings, offer_id: str, token: str, now: datetime) -> Offer:
    offer = _load(db, settings, offer_id, token)
    lead = _lock_lead(db, offer.lead_id)
    db.refresh(offer)  # re-read now that we hold the lock
    if _expire_if_due(db, settings, offer, now):
        db.commit()
        raise OfferError(409, "this offer has expired")
    if offer.status != OfferStatus.PENDING:
        raise _closed(offer)

    # Compare-and-set: "give me the lead ONLY IF nobody owns it yet".
    won = db.execute(
        update(Lead)
        .where(Lead.id == lead.id, Lead.assigned_attorney_id.is_(None))
        .values(assigned_attorney_id=offer.attorney_id, status=LeadStatus.ASSIGNED, assigned_at=now)
    ).rowcount
    if not won:
        db.rollback()
        raise OfferError(409, "another attorney claimed this lead first")

    db.execute(
        update(Offer)
        .where(Offer.id == offer.id)
        .values(status=OfferStatus.ACCEPTED, responded_at=now)
    )
    db.execute(
        update(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.id != offer.id,
            Offer.status.in_([OfferStatus.PENDING, OfferStatus.QUEUED]),
        )
        .values(status=OfferStatus.LOST, responded_at=now)
    )
    db.refresh(offer)
    db.refresh(lead)
    finalize_assignment(db, settings, lead, offer, now)
    db.commit()
    return offer


def decline(
    db: Session, settings: Settings, offer_id: str, token: str, reason: str | None, now: datetime
) -> Offer:
    offer = _load(db, settings, offer_id, token)
    lead = _lock_lead(db, offer.lead_id)
    db.refresh(offer)
    if _expire_if_due(db, settings, offer, now):
        db.commit()
        raise OfferError(409, "this offer has expired")
    if offer.status != OfferStatus.PENDING:
        raise _closed(offer)

    offer.status = OfferStatus.DECLINED
    offer.responded_at = now
    offer.decline_reason = reason
    db.flush()
    audit.record(
        db,
        lead.id,
        "offer_declined",
        attorney_id=offer.attorney_id,
        round=offer.round,
        reason=reason,
    )
    advance(db, settings, lead, now)
    db.commit()
    return offer


def expire_due(db: Session, settings: Settings, now: datetime) -> int:
    """Called by the worker every few seconds: time out offers nobody answered."""
    due = db.scalars(
        select(Offer)
        .where(Offer.status == OfferStatus.PENDING, Offer.expires_at <= now)
        .order_by(Offer.expires_at)
    ).all()
    expired = 0
    for offer in due:
        expired += _expire_if_due(db, settings, offer, now)
        db.commit()  # one lead at a time keeps lock time short
    return expired


def view(db: Session, settings: Settings, offer_id: str, token: str, now: datetime) -> OfferView:
    offer = _load(db, settings, offer_id, token)
    if _expire_if_due(db, settings, offer, now):
        db.commit()
    lead = db.get(Lead, offer.lead_id)

    notices = {
        OfferStatus.QUEUED: "You're in line for this lead. We'll email you if it reaches you.",
        OfferStatus.PENDING: "Accept to receive the person's contact details.",
        OfferStatus.ACCEPTED: "This lead is yours. Please contact them promptly.",
        OfferStatus.DECLINED: "You passed on this lead.",
        OfferStatus.EXPIRED: "This offer expired before it was answered.",
        OfferStatus.LOST: "Another attorney claimed this lead first.",
        OfferStatus.SKIPPED: "This lead moved on because you were unavailable when it reached you.",
    }
    contact = None
    if offer.status == OfferStatus.ACCEPTED:
        contact = {
            "name": f"{lead.first_name} {lead.last_name}",
            "phone": lead.phone,
            "email": lead.email,
            "description": lead.description,
            "incident_date": lead.incident_date.isoformat() if lead.incident_date else None,
        }
    return OfferView(
        offer_id=offer.id,
        status=offer.status,
        model=lead.distribution_model,
        practice_area=lead.practice_area,
        zip_code=lead.zip_code,
        state=lead.state,
        score=lead.score or 0,
        tier=lead.tier or "-",
        summary=lead.summary(),
        price_cents=offer.price_cents,
        expires_at=offer.expires_at,
        contact=contact,
        notice=notices[OfferStatus(offer.status)],
    )
