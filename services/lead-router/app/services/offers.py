"""Offers: asking attorneys "do you want this lead?" and handling their answers.

The life of an offer:

    queued ──► pending ──► accepted   (attorney said yes: lead is theirs)
                  │
                  ├──────► declined   (attorney said no)
                  ├──────► expired    (attorney didn't answer in time)
                  ├──────► skipped    (attorney inactive or at their daily cap)
                  └──────► lost       (someone else said yes first)

The tricky part is two people changing the same lead at the same moment: two
attorneys clicking "accept", or an accept racing the worker's timeout. Two
rules keep that safe:

1. Every change is a compare-and-set: "mark this offer accepted ONLY IF it is
   still pending", "give the lead to me ONLY IF nobody owns it yet". If the
   world changed underneath us, the update touches zero rows and we stop.
2. Locks are always taken in the same order (lead row, then attorney row), so
   two requests can never wait on each other forever (no deadlocks).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.types import DistributionModel, LeadStatus, OfferStatus
from app.models import Attorney, Lead, Offer
from app.schemas import OfferView
from app.services import audit, billing, emails, exo_sync, links, outbox


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


def start_of_day(now: datetime, timezone: str) -> datetime:
    """Midnight in the business's timezone, as a UTC timestamp."""
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def accepted_today(
    db: Session, settings: Settings, now: datetime, attorney_id: int | None = None
) -> dict[int, int]:
    stmt = (
        select(Offer.attorney_id, func.count())
        .where(
            Offer.status == OfferStatus.ACCEPTED,
            Offer.responded_at >= start_of_day(now, settings.business_timezone),
        )
        .group_by(Offer.attorney_id)
    )
    if attorney_id is not None:
        stmt = stmt.where(Offer.attorney_id == attorney_id)
    return {attorney: count for attorney, count in db.execute(stmt).all()}


def credentials_problem(settings: Settings, attorney: Attorney, now: datetime) -> str | None:
    """A recent bar-license check, and the current participation agreement if there is one.
    Checked when the line-up is made, when each attorney's turn comes, and at acceptance."""
    if settings.require_license_verification and (
        attorney.license_verified_at is None
        or attorney.license_verified_at < now - timedelta(days=settings.license_recheck_days)
    ):
        return "bar license not checked recently"
    wanted = settings.attorney_agreement_version
    if wanted and attorney.agreement_version != wanted:
        return "hasn't accepted the current participation agreement"
    return None


def unavailable_reason(
    db: Session, settings: Settings, attorney: Attorney, now: datetime
) -> str | None:
    """Re-checked at the moment of acceptance, not just when the offer was made."""
    if not attorney.active:
        return "inactive"
    if problem := credentials_problem(settings, attorney, now):
        return problem
    taken = accepted_today(db, settings, now, attorney.id).get(attorney.id, 0)
    if taken >= attorney.daily_cap:
        return "daily lead cap reached"
    return None


# --------------------------------------------------------------------------- #
# Side effects (all go through the outbox)
# --------------------------------------------------------------------------- #
def notify_offer(db: Session, settings: Settings, lead: Lead, offer: Offer) -> None:
    link = links.offer_link(settings.public_base_url, settings.link_signing_secret, offer.id)
    attorney = db.get(Attorney, offer.attorney_id)
    outbox.enqueue(
        db, "email", emails.offer_email(settings, lead, offer, attorney, link), lead_id=lead.id
    )


def finalize_assignment(
    db: Session, settings: Settings, lead: Lead, offer: Offer, now: datetime
) -> None:
    """Everything that happens once a lead belongs to an attorney."""
    attorney = db.get(Attorney, offer.attorney_id)
    billing.charge_for_delivery(db, offer, lead, now)
    outbox.enqueue(
        db, "email", emails.delivered_email(settings, lead, offer, attorney), lead_id=lead.id
    )
    # The person is told who has their details, so they can check the attorney's license,
    # tell them to stop, or ask us to delete their information.
    outbox.enqueue(db, "email", emails.consumer_assigned(settings, lead, attorney), lead_id=lead.id)
    if settings.exo_sync_enabled and attorney.exo_username:
        email = exo_sync.exo_email(lead, settings.exo_sync_contact_details)
        outbox.enqueue(
            db,
            "exo.assign_lead",
            {"email": email, "assignee": attorney.exo_username},
            lead_id=lead.id,
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
# Moving offers along
# --------------------------------------------------------------------------- #
def _set_status(db: Session, offer: Offer, expected: str, new: str, **values) -> bool:
    """Compare-and-set one offer's status. Returns False if someone else changed it first."""
    changed = db.execute(
        update(Offer)
        .where(Offer.id == offer.id, Offer.status == expected)
        .values(status=new, **values)
    ).rowcount
    if changed:
        db.refresh(offer)
    return bool(changed)


def activate(db: Session, settings: Settings, lead: Lead, offer: Offer, now: datetime) -> bool:
    expires = now + ttl_for(settings, lead.distribution_model)
    if not _set_status(
        db, offer, OfferStatus.QUEUED, OfferStatus.PENDING, activated_at=now, expires_at=expires
    ):
        return False
    notify_offer(db, settings, lead, offer)
    return True


_CONSENTED_RE = re.compile(r"up to ([0-9]+) participating")


def consent_limit(settings: Settings, lead: Lead) -> int:
    """How many attorneys this person agreed to: the number stored with their consent."""
    if lead.max_attorneys is not None:
        return lead.max_attorneys
    # Leads from before the number was stored: it's in the consent text they agreed to.
    agreed = _CONSENTED_RE.search(lead.consent_text or "")
    if agreed:
        return min(int(agreed.group(1)), settings.shared_max_attorneys)
    return settings.shared_max_attorneys


def _asked(db: Session, lead: Lead) -> int:
    # An attorney has been "asked" once their offer was activated (shown to them).
    return db.scalar(
        select(func.count()).where(Offer.lead_id == lead.id, Offer.activated_at.is_not(None))
    )


def close_line_up_if_full(db: Session, settings: Settings, lead: Lead, now: datetime) -> None:
    """Once the consented number of attorneys has been asked, nobody else ever can be:
    close the rest of the line in one statement, with one audit entry."""
    asked = _asked(db, lead)
    if asked < consent_limit(settings, lead):
        return
    waiting = db.execute(
        select(Offer.id, Attorney.name)
        .join(Attorney, Attorney.id == Offer.attorney_id)
        .where(Offer.lead_id == lead.id, Offer.status == OfferStatus.QUEUED)
        .order_by(Offer.round)
    ).all()
    if not waiting:
        return
    reason = f"line-up full ({asked} attorneys asked)"
    db.execute(
        update(Offer)
        .where(Offer.id.in_([offer_id for offer_id, _ in waiting]))
        .where(Offer.status == OfferStatus.QUEUED)
        .values(status=OfferStatus.SKIPPED, responded_at=now, decline_reason=reason)
    )
    audit.record(db, lead.id, "line_up_full", reason=reason, not_asked=[n for _, n in waiting])


WITHDRAWN = "withdrawn: no longer accepted for this state or matter"


def withdraw_if_closed(db: Session, settings: Settings, lead: Lead, now: datetime) -> bool:
    """The state or practice area was closed while the lead was still on offer (launch
    states, counsel clearances or blocks changed): nobody else is asked, nobody can accept,
    and the lead ends unsold. True if that happened."""
    reason = settings.closed_reason(lead.state, lead.practice_area)
    if not reason or lead.status != LeadStatus.OFFERED:
        return False
    db.execute(
        update(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.status.in_([OfferStatus.PENDING, OfferStatus.QUEUED]),
        )
        .values(status=OfferStatus.SKIPPED, responded_at=now, decline_reason=WITHDRAWN)
    )
    went_unsold = db.execute(
        update(Lead)
        .where(
            Lead.id == lead.id,
            Lead.status == LeadStatus.OFFERED,
            Lead.assigned_attorney_id.is_(None),
        )
        .values(status=LeadStatus.UNSOLD)
    ).rowcount
    if went_unsold:
        db.refresh(lead)
        audit.record(db, lead.id, "withdrawn", reason=reason)
    return True


def advance(db: Session, settings: Settings, lead: Lead, now: datetime) -> None:
    """After a decline, timeout or skip: ask the next attorney, or give up if no one is left."""
    if lead.status != LeadStatus.OFFERED:
        return
    if withdraw_if_closed(db, settings, lead, now):
        return
    still_waiting = db.scalar(
        select(func.count()).where(Offer.lead_id == lead.id, Offer.status == OfferStatus.PENDING)
    )
    if still_waiting:
        return

    if lead.distribution_model == DistributionModel.PING_TREE:
        # The consent covers N attorneys. Only attorneys actually asked count, so one
        # skipped below (never shown the lead) doesn't use up a place.
        close_line_up_if_full(db, settings, lead, now)
        counts = accepted_today(db, settings, now)
        queued = db.scalars(
            select(Offer)
            .where(Offer.lead_id == lead.id, Offer.status == OfferStatus.QUEUED)
            .order_by(Offer.round)
        ).all()
        for nxt in queued:
            attorney = db.get(Attorney, nxt.attorney_id)
            # Things may have changed since the line-up was made.
            if not attorney.active:
                reason = "inactive"
            elif problem := credentials_problem(settings, attorney, now):
                reason = problem
            elif counts.get(attorney.id, 0) >= attorney.daily_cap:
                reason = "daily lead cap reached"
            else:
                reason = None
            if reason:
                if _set_status(
                    db,
                    nxt,
                    OfferStatus.QUEUED,
                    OfferStatus.SKIPPED,
                    responded_at=now,
                    decline_reason=reason,
                ):
                    audit.record(
                        db,
                        lead.id,
                        "offer_skipped",
                        attorney=attorney.name,
                        round=nxt.round,
                        reason=reason,
                    )
                continue
            if activate(db, settings, lead, nxt, now):
                audit.record(
                    db, lead.id, "offer_activated", attorney=attorney.name, round=nxt.round
                )
                close_line_up_if_full(db, settings, lead, now)
                return

    went_unsold = db.execute(
        update(Lead)
        .where(
            Lead.id == lead.id,
            Lead.status == LeadStatus.OFFERED,
            Lead.assigned_attorney_id.is_(None),
        )
        .values(status=LeadStatus.UNSOLD)
    ).rowcount
    if went_unsold:
        db.refresh(lead)
        audit.record(db, lead.id, "unsold", reason="every attorney passed or ran out of time")


# --------------------------------------------------------------------------- #
# Attorney actions
# --------------------------------------------------------------------------- #
def _load(db: Session, settings: Settings, offer_id: str, token: str) -> Offer:
    # Same answer for "doesn't exist" and "bad link" so links can't be probed.
    if not links.verify_offer(settings.link_signing_secret, offer_id, token):
        raise OfferError(404, "offer not found")
    offer = db.get(Offer, offer_id)
    if offer is None:
        raise OfferError(404, "offer not found")
    return offer


def _lock(db: Session, model, key):
    """SELECT ... FOR UPDATE, always fresh from the database.

    Lock order everywhere: the lead row first, then the attorney row. On SQLite
    FOR UPDATE is a no-op; the compare-and-set updates still keep it correct.
    """
    return db.scalars(
        select(model)
        .where(model.id == key)
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
    lead = _lock(db, Lead, offer.lead_id)
    if not _set_status(db, offer, OfferStatus.PENDING, OfferStatus.EXPIRED, responded_at=now):
        return False
    audit.record(db, lead.id, "offer_expired", attorney_id=offer.attorney_id, round=offer.round)
    advance(db, settings, lead, now)
    return True


def accept(db: Session, settings: Settings, offer_id: str, token: str, now: datetime) -> Offer:
    offer = _load(db, settings, offer_id, token)
    lead = _lock(db, Lead, offer.lead_id)
    db.refresh(offer)  # re-read now that we hold the lock
    if _expire_if_due(db, settings, offer, now):
        db.commit()
        raise OfferError(409, "this offer has expired")
    if offer.status != OfferStatus.PENDING:
        raise _closed(offer)
    if withdraw_if_closed(db, settings, lead, now):
        db.commit()
        raise OfferError(409, "this lead is no longer available")

    attorney = _lock(db, Attorney, offer.attorney_id)
    if reason := unavailable_reason(db, settings, attorney, now):
        if _set_status(
            db,
            offer,
            OfferStatus.PENDING,
            OfferStatus.SKIPPED,
            responded_at=now,
            decline_reason=reason,
        ):
            audit.record(db, lead.id, "offer_skipped", attorney=attorney.name, reason=reason)
            advance(db, settings, lead, now)
        db.commit()
        raise OfferError(409, f"you can't take this lead right now: {reason}")

    # Compare-and-set: "give me the lead ONLY IF nobody owns it yet".
    won = db.execute(
        update(Lead)
        .where(Lead.id == lead.id, Lead.assigned_attorney_id.is_(None))
        .values(assigned_attorney_id=offer.attorney_id, status=LeadStatus.ASSIGNED, assigned_at=now)
    ).rowcount
    if not won:
        db.rollback()
        raise OfferError(409, "another attorney claimed this lead first")
    if not _set_status(db, offer, OfferStatus.PENDING, OfferStatus.ACCEPTED, responded_at=now):
        db.rollback()
        raise OfferError(409, "this offer is no longer available")

    db.execute(
        update(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.id != offer.id,
            Offer.status.in_([OfferStatus.PENDING, OfferStatus.QUEUED]),
        )
        .values(status=OfferStatus.LOST, responded_at=now)
    )
    db.refresh(lead)
    finalize_assignment(db, settings, lead, offer, now)
    db.commit()
    return offer


def decline(
    db: Session, settings: Settings, offer_id: str, token: str, reason: str | None, now: datetime
) -> Offer:
    offer = _load(db, settings, offer_id, token)
    lead = _lock(db, Lead, offer.lead_id)
    db.refresh(offer)
    if _expire_if_due(db, settings, offer, now):
        db.commit()
        raise OfferError(409, "this offer has expired")
    if not _set_status(
        db,
        offer,
        OfferStatus.PENDING,
        OfferStatus.DECLINED,
        responded_at=now,
        decline_reason=reason,
    ):
        db.refresh(offer)
        raise _closed(offer)

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


NOTICES = {
    OfferStatus.QUEUED: "You're in line for this lead. We'll email you if it reaches you.",
    OfferStatus.PENDING: "Accept to receive the person's contact details.",
    OfferStatus.ACCEPTED: "This lead is yours. Please contact them promptly.",
    OfferStatus.DECLINED: "You passed on this lead.",
    OfferStatus.EXPIRED: "This offer expired before it was answered.",
    OfferStatus.LOST: "Another attorney claimed this lead first.",
    OfferStatus.SKIPPED: "This lead moved on because you were unavailable (inactive or at your "
    "daily limit).",
}


def view(db: Session, settings: Settings, offer_id: str, token: str, now: datetime) -> OfferView:
    offer = _load(db, settings, offer_id, token)
    if _expire_if_due(db, settings, offer, now):
        db.commit()
    lead = db.get(Lead, offer.lead_id)

    contact = None
    if offer.status == OfferStatus.ACCEPTED:
        contact = {
            "name": f"{lead.first_name} {lead.last_name}",
            "phone": lead.phone,
            "email": lead.email,
            "description": lead.description,
            "incident_date": lead.incident_date.isoformat() if lead.incident_date else None,
        }
    seconds_left = None
    if offer.status == OfferStatus.PENDING and offer.expires_at:
        seconds_left = max(0, int((offer.expires_at - now).total_seconds()))
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
        seconds_left=seconds_left,
        contact=contact,
        notice=_notice(offer),
        price_note=offer.price_note,
    )


CREDENTIAL_REASONS = ("bar license not checked recently", "hasn't accepted the current")


def _notice(offer: Offer) -> str:
    reason = offer.decline_reason or ""
    if offer.status == OfferStatus.SKIPPED:
        if reason.startswith("line-up"):
            return "This lead was offered to the agreed number of attorneys before your turn came."
        if reason.startswith("withdrawn"):
            return "This inquiry was withdrawn, so it's no longer available."
        if reason.startswith(CREDENTIAL_REASONS):
            return (
                "This lead moved on because your bar license check or participation agreement "
                "needs renewing. Please contact us."
            )
    return NOTICES[OfferStatus(offer.status)]
