"""Database tables.

attorneys ──< territories            (an attorney can rent exclusive ZIPs)
attorneys ──< offers >── leads        (a lead is offered to one or more attorneys)
leads ──< audit_events                (every decision, written down)
attorneys ──< ledger_entries          (what each attorney owes, flat fees only)
outbox_messages                       (emails and eXo updates waiting to be sent)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow
from app.domain import compliance

# none_as_null: a Python None becomes SQL NULL (caught by NOT NULL) instead of JSON 'null'.
JSONField = JSON(none_as_null=True)


def new_id() -> str:
    return str(uuid.uuid4())


class Attorney(Base):
    __tablename__ = "attorneys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    firm: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(20))
    bar_states: Mapped[list[str]] = mapped_column(JSONField)
    service_zips: Mapped[list[str]] = mapped_column(JSONField)
    bids_cents: Mapped[dict[str, int]] = mapped_column(JSONField)
    plan: Mapped[str] = mapped_column(String(20))
    monthly_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    daily_cap: Mapped[int] = mapped_column(Integer, default=10)
    exo_username: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped each time a bid link is sent; only the newest link works (services/links.py).
    bids_link_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    territories: Mapped[list[Territory]] = relationship(back_populates="attorney")


class Territory(Base):
    __tablename__ = "territories"
    # One owner per ZIP + practice area: that's what "exclusive" means.
    __table_args__ = (UniqueConstraint("practice_area", "zip_code", name="uq_territory"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attorney_id: Mapped[int] = mapped_column(ForeignKey("attorneys.id"))
    practice_area: Mapped[str] = mapped_column(String(40))
    zip_code: Mapped[str] = mapped_column(String(5))
    monthly_fee_cents: Mapped[int] = mapped_column(Integer)

    attorney: Mapped[Attorney] = relationship(back_populates="territories")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    # Who and where
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(254), index=True)
    phone: Mapped[str] = mapped_column(String(10), index=True)
    zip_code: Mapped[str] = mapped_column(String(5))
    state: Mapped[str] = mapped_column(String(2))

    # What happened
    practice_area: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    incident_date: Mapped[date | None] = mapped_column(Date)
    injured: Mapped[bool | None] = mapped_column(Boolean)
    medical_treatment: Mapped[bool | None] = mapped_column(Boolean)
    has_attorney: Mapped[bool] = mapped_column(Boolean, default=False)
    court_date_soon: Mapped[bool] = mapped_column(Boolean, default=False)

    # Proof of consent (who agreed to what, when, from where)
    consent: Mapped[bool] = mapped_column(Boolean)
    consent_version: Mapped[str] = mapped_column(String(40))
    consent_text: Mapped[str] = mapped_column(Text)
    # The "up to N attorneys" in that text. Stored so a later change to the setting can't
    # widen what this person agreed to. Empty only on leads from before it was recorded.
    max_attorneys: Mapped[int | None] = mapped_column(Integer)
    consent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    utm_source: Mapped[str | None] = mapped_column(String(120))

    # What the system decided
    status: Mapped[str] = mapped_column(String(20), index=True)
    rejection_reasons: Mapped[list[str]] = mapped_column(JSONField, default=list)
    score: Mapped[int | None] = mapped_column(Integer)
    tier: Mapped[str | None] = mapped_column(String(1))
    score_breakdown: Mapped[list[dict[str, Any]]] = mapped_column(JSONField, default=list)
    flags: Mapped[list[str]] = mapped_column(JSONField, default=list)
    distribution_model: Mapped[str | None] = mapped_column(String(20))
    assigned_attorney_id: Mapped[int | None] = mapped_column(ForeignKey("attorneys.id"))
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    offers: Mapped[list[Offer]] = relationship(back_populates="lead", order_by="Offer.round")
    audit: Mapped[list[AuditEvent]] = relationship(order_by="AuditEvent.id")

    def summary(self) -> str:
        """The description with contact details redacted, shortened for offer previews.

        This is what attorneys see before they accept. The full description is only
        released with the contact details, after acceptance.
        """
        text = compliance.redact(
            self.description,
            names=(self.first_name, self.last_name),
            email=self.email,
            phone=self.phone,
        )
        text = " ".join(text.split())
        return text if len(text) <= 280 else text[:277] + "..."


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    attorney_id: Mapped[int] = mapped_column(ForeignKey("attorneys.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decline_reason: Mapped[str | None] = mapped_column(String(200))
    # How the price was set, in words, written when the offer is priced (market.price_note).
    price_note: Mapped[str | None] = mapped_column(String(200))

    lead: Mapped[Lead] = relationship(back_populates="offers")
    attorney: Mapped[Attorney] = relationship()


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    # The same charge can never be written twice (safe to re-run billing).
    __table_args__ = (
        UniqueConstraint("attorney_id", "kind", "period", "reference", name="uq_ledger_charge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    attorney_id: Mapped[int] = mapped_column(ForeignKey("attorneys.id"), index=True)
    lead_id: Mapped[str | None] = mapped_column(ForeignKey("leads.id"))
    kind: Mapped[str] = mapped_column(String(20))
    amount_cents: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(7), index=True)  # "YYYY-MM"
    reference: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    event: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONField, default=dict)


class OutboxMessage(Base):
    """A side effect (email, eXo update) saved in the same transaction as the change that caused it.

    A worker sends these afterwards and retries on failure. If the database
    change rolls back, the message disappears with it, so we never email an
    attorney about a lead that doesn't exist.
    """

    __tablename__ = "outbox_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONField)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
