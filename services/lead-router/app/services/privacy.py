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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain import compliance
from app.domain.types import LeadStatus, OfferStatus
from app.models import Attorney, AuditEvent, Lead, Offer, OutboxMessage
from app.services import audit, emails, outbox

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


def _scrub_outbox(db: Session, lead: Lead) -> int:
    """Blank every queued or sent message that mentions this person. Unsent ones are
    cancelled, so nothing about them goes out after they asked us to erase it."""
    # Offer emails carry the redacted summary and the offer's id, not the person's details.
    offer_ids = db.scalars(select(Offer.id).where(Offer.lead_id == lead.id)).all()
    needles = [n for n in (lead.email, lead.phone, lead.id, *offer_ids) if n]
    scrubbed = 0
    # Messages about a lead are written while it's routed, which takes minutes, not days.
    window = (lead.created_at, lead.created_at + timedelta(days=7))
    for message in db.scalars(
        select(OutboxMessage).where(OutboxMessage.created_at.between(*window))
    ):
        if message.payload.get("erased"):
            continue
        text = json.dumps(message.payload)
        if any(needle in text for needle in needles):
            message.payload = {"erased": True, "kind": message.kind}
            if message.sent_at is None:
                message.failed = True
                message.last_error = "cancelled: the person's data was erased"
            scrubbed += 1
    return scrubbed


def _withdraw(db: Session, lead: Lead, now: datetime) -> None:
    """Stop offering a lead that's still out: nobody new gets it after an erase."""
    if lead.status != LeadStatus.OFFERED:
        return
    db.execute(
        update(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.status.in_([OfferStatus.PENDING, OfferStatus.QUEUED]),
        )
        .values(status=OfferStatus.SKIPPED, responded_at=now, decline_reason="withdrawn")
    )
    lead.status = LeadStatus.UNSOLD
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
    if lead.legal_hold:
        raise PrivacyError(409, "this lead is under a legal hold; lift it first")
    if level not in (NARRATIVE, FULL):
        raise PrivacyError(422, "level must be 'narrative' or 'full'")
    _withdraw(db, lead, now)
    scrubbed = _scrub_outbox(db, lead)
    if lead.narrative_erased_at is None:
        lead.description = ERASED
        lead.injured = lead.medical_treatment = lead.incident_date = None
        lead.has_attorney = lead.court_date_soon = False
        lead.flags, lead.score_breakdown = [], []
        lead.user_agent = None
        lead.utm_source = lead.utm_medium = lead.utm_campaign = lead.utm_content = None
        lead.referrer_host = lead.landing_path = None
        lead.narrative_erased_at = now
        # Attorneys' pass reasons are free text and may repeat what the person said.
        db.execute(
            update(Offer)
            .where(Offer.lead_id == lead.id, Offer.decline_reason.is_not(None))
            .values(decline_reason=ERASED)
        )
        for event in db.scalars(select(AuditEvent).where(AuditEvent.lead_id == lead.id)):
            if event.event == "offer_declined" and event.detail.get("reason"):
                event.detail = {**event.detail, "reason": ERASED}
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
    audit.record(db, lead.id, "erased", level=level, reason=reason, emails_scrubbed=scrubbed)


def set_hold(db: Session, lead: Lead, on: bool, reason: str) -> None:
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
        for lead in due:
            erase(db, settings, lead, level, now, reason=f"retention: {name}")
        counts[name] = len(due)
    # Bodies of sent emails: they repeat lead details and are only needed for a few days.
    old = db.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.sent_at.is_not(None),
            OutboxMessage.sent_at < now - settings.retention_outbox_days * days,
        )
        .limit(BATCH * 5)
    ).all()
    counts["sent_email"] = 0
    for message in old:
        if not message.payload.get("erased"):
            message.payload = {"erased": True, "kind": message.kind}
            counts["sent_email"] += 1
    db.commit()
    return counts
