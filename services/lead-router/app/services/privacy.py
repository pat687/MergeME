"""People's data: finding it, erasing it on request, and erasing it on schedule.

Two levels of erasing, because some details must outlive others:

* narrative - the story and anything health- or matter-related: the description,
  injury and treatment answers, incident date, flags, the score's reasons, the
  browser, where the visit came from, attorneys' pass reasons, and every copy
  in the outbox. Done on request, and automatically some weeks after delivery.
* full      - also the name, email, phone and IP address. What's left is the
  consent wording (with the number blanked), timestamps, the practice area and
  the routing record. Done for leads that were never sold soon after they
  closed, and for sold leads once the consent evidence is no longer needed to
  answer a contact-law claim (settings.retention_evidence_days).

A legal hold stops both, for one lead, until it's lifted.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain import compliance
from app.domain.types import LeadStatus, OfferStatus
from app.models import Attorney, AuditEvent, Lead, Offer, OutboxMessage
from app.services import audit, emails, outbox

log = logging.getLogger(__name__)

ERASED = "[erased]"
NARRATIVE = "narrative"
FULL = "full"
# How many leads each retention pass handles per worker cycle, so one cycle stays short.
BATCH = 200


class PrivacyError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code, self.message = status_code, message


def find(db: Session, email: str | None, phone: str | None) -> list[Lead]:
    """Every lead matching this email or phone, newest first."""
    conditions = []
    if email:
        conditions.append(Lead.email == email.strip().lower())
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        conditions.append(Lead.phone == digits)
    if not conditions:
        return []
    return list(db.scalars(select(Lead).where(or_(*conditions)).order_by(Lead.created_at.desc())))


def export(db: Session, lead: Lead) -> dict[str, Any]:
    """Everything we hold about one lead, for an access request."""
    columns = {c.key: getattr(lead, c.key) for c in Lead.__mapper__.column_attrs}
    offers = db.scalars(select(Offer).where(Offer.lead_id == lead.id).order_by(Offer.round))
    names = {a.id: f"{a.name}, {a.firm}" for a in db.scalars(select(Attorney))}
    return {
        "lead": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in columns.items()},
        "sent_to": [
            {
                "attorney": names.get(o.attorney_id),
                "status": o.status,
                "offered_at": o.activated_at.isoformat() if o.activated_at else None,
            }
            for o in offers
            if o.activated_at or o.status == OfferStatus.ACCEPTED
        ],
        "history": [
            {"at": e.at.isoformat(), "event": e.event}
            for e in db.scalars(select(AuditEvent).where(AuditEvent.lead_id == lead.id))
        ],
    }


def _blank(message: OutboxMessage, now: datetime) -> None:
    message.payload = {"erased": True, "kind": message.kind}
    message.payload_erased_at = now
    if message.sent_at is None:
        message.failed = True
        message.last_error = "cancelled: the person's data was erased"


def _scrub_outbox(db: Session, lead: Lead, now: datetime) -> int:
    """Blank this lead's queued and sent messages. Unsent ones are cancelled, so nothing
    about the person goes out after they asked us to erase it."""
    pending = select(OutboxMessage).where(OutboxMessage.payload_erased_at.is_(None))
    messages = list(db.scalars(pending.where(OutboxMessage.lead_id == lead.id)))
    # Messages written before they carried a lead id: matched by content, within the
    # week the lead was being routed. Offer emails carry the offer's id, not the person's.
    offer_ids = db.scalars(select(Offer.id).where(Offer.lead_id == lead.id)).all()
    needles = [lead.email, compliance.phone_display(lead.phone), lead.id, *offer_ids]
    window = (lead.created_at, lead.created_at + timedelta(days=7))
    for message in db.scalars(
        pending.where(OutboxMessage.lead_id.is_(None), OutboxMessage.created_at.between(*window))
    ):
        text = json.dumps(message.payload, ensure_ascii=False)
        if any(needle and needle in text for needle in needles):
            messages.append(message)
    for message in messages:
        _blank(message, now)
    return len(messages)


def _lock(db: Session, lead_id: str) -> Lead:
    """Fresh and locked: an erase can't race an attorney accepting the lead (offers.accept
    takes the same lock), and a hold placed a moment ago is seen."""
    return db.scalars(
        select(Lead)
        .where(Lead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _withdraw(db: Session, lead: Lead, now: datetime) -> None:
    """Stop offering a lead that's still out: nobody new gets it after an erase."""
    went_unsold = db.execute(
        update(Lead)
        .where(
            Lead.id == lead.id,
            Lead.status == LeadStatus.OFFERED,
            Lead.assigned_attorney_id.is_(None),
        )
        .values(status=LeadStatus.UNSOLD)
    ).rowcount
    if not went_unsold:
        return
    db.execute(
        update(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.status.in_([OfferStatus.PENDING, OfferStatus.QUEUED]),
        )
        .values(status=OfferStatus.SKIPPED, responded_at=now, decline_reason="withdrawn")
    )
    db.refresh(lead)
    audit.record(db, lead.id, "withdrawn", reason="the person's details were erased")


def erase(
    db: Session,
    settings: Settings,
    lead: Lead,
    level: str,
    now: datetime,
    *,
    reason: str,
    notify_attorneys: bool = False,
) -> None:
    """Erase one lead at the given level. Raises PrivacyError under a legal hold."""
    lead = _lock(db, lead.id)
    if lead.legal_hold:
        raise PrivacyError(409, "this lead is under a legal hold; lift it first")
    if level not in (NARRATIVE, FULL):
        raise PrivacyError(422, "level must be 'narrative' or 'full'")
    _withdraw(db, lead, now)
    scrubbed = _scrub_outbox(db, lead, now)
    if lead.narrative_erased_at is None:
        lead.description = ERASED
        lead.injured = lead.medical_treatment = lead.incident_date = None
        lead.has_attorney = lead.court_date_soon = False
        lead.flags, lead.score_breakdown = [], []
        lead.user_agent = None
        lead.utm_source = lead.utm_medium = lead.utm_campaign = lead.utm_content = None
        lead.referrer_host = lead.landing_path = None
        lead.narrative_erased_at = now
        # Attorneys' pass reasons are free text and may repeat what the person said; the
        # system's own skip reasons (line-up full, withdrawn) stay.
        db.execute(
            update(Offer)
            .where(
                Offer.lead_id == lead.id,
                Offer.status == OfferStatus.DECLINED,
                Offer.decline_reason.is_not(None),
            )
            .values(decline_reason=ERASED)
        )
        for event in db.scalars(select(AuditEvent).where(AuditEvent.lead_id == lead.id)):
            if event.event == "offer_declined" and event.detail.get("reason"):
                event.detail = {**event.detail, "reason": ERASED}
            elif event.event == "scored" and event.detail.get("flags"):
                event.detail = {**event.detail, "flags": []}
    if level == FULL and lead.erased_at is None:
        number = compliance.phone_display(lead.phone) if lead.phone else None
        if number and number in lead.consent_text:
            lead.consent_text = lead.consent_text.replace(number, ERASED)
        lead.first_name = lead.last_name = ERASED
        lead.email = f"erased-{lead.id[:8]}@erased.invalid"
        lead.phone = "0000000000"
        lead.ip_address = None
        lead.erased_at = now
    if notify_attorneys:
        # Whoever received the contact details is asked to delete them too.
        for offer in db.scalars(
            select(Offer).where(Offer.lead_id == lead.id, Offer.status == OfferStatus.ACCEPTED)
        ):
            attorney = db.get(Attorney, offer.attorney_id)
            outbox.enqueue(
                db, "email", emails.deletion_request(settings, lead.id[:8].upper(), attorney)
            )
    if settings.exo_sync_enabled and settings.exo_sync_contact_details:
        log.warning("lead %s erased here; delete its eXo copy by hand", lead.id)
    audit.record(db, lead.id, "erased", level=level, reason=reason, emails_scrubbed=scrubbed)


def set_hold(db: Session, lead: Lead, on: bool, reason: str) -> None:
    lead = _lock(db, lead.id)
    lead.legal_hold = on
    lead.legal_hold_reason = reason if on else None
    audit.record(db, lead.id, "legal_hold_on" if on else "legal_hold_off", reason=reason)


def purge_due(db: Session, settings: Settings, now: datetime) -> dict[str, int]:
    """The retention schedule (settings.retention_*), run by the worker. Skips held leads."""
    days = timedelta(days=1)
    not_held = or_(Lead.legal_hold.is_(None), Lead.legal_hold.is_(False))
    passes = [
        # (what, level, which leads)
        (
            "rejected",
            FULL,
            (Lead.status == LeadStatus.REJECTED)
            & (Lead.created_at < now - settings.retention_rejected_days * days),
        ),
        (
            "unrouted",
            FULL,
            Lead.status.in_([LeadStatus.UNSERVICEABLE, LeadStatus.UNSOLD])
            & (Lead.created_at < now - settings.retention_unrouted_days * days),
        ),
        (
            "narrative",
            NARRATIVE,
            (Lead.status == LeadStatus.ASSIGNED)
            & (Lead.assigned_at < now - settings.retention_narrative_days * days)
            & Lead.narrative_erased_at.is_(None),
        ),
        (
            "evidence",
            FULL,
            (Lead.status == LeadStatus.ASSIGNED)
            & (Lead.assigned_at < now - settings.retention_evidence_days * days),
        ),
    ]
    counts: dict[str, int] = {}
    for name, level, condition in passes:
        due = db.scalars(
            select(Lead).where(condition, Lead.erased_at.is_(None), not_held).limit(BATCH)
        ).all()
        counts[name] = 0
        for lead in due:
            try:
                erase(db, settings, lead, level, now, reason=f"retention: {name}")
                counts[name] += 1
            except PrivacyError:
                continue  # put under a legal hold after it was picked
    # Bodies of sent emails: they repeat lead details and are only needed for a few days.
    # Never those of a lead under a legal hold: they show what was actually sent.
    held = select(Lead.id).where(Lead.legal_hold.is_(True))
    old = db.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.sent_at.is_not(None),
            OutboxMessage.sent_at < now - settings.retention_outbox_days * days,
            OutboxMessage.payload_erased_at.is_(None),
            or_(OutboxMessage.lead_id.is_(None), OutboxMessage.lead_id.not_in(held)),
        )
        .order_by(OutboxMessage.sent_at)
        .limit(BATCH * 5)
    ).all()
    for message in old:
        _blank(message, now)
    counts["sent_email"] = len(old)
    db.commit()
    return counts
