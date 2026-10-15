"""Money in: flat fees only, written to an append-only ledger.

Two ways an attorney pays:
* Pay per lead - a flat price each time they accept a lead.
* Subscription - a flat monthly fee (plus flat monthly fees for any exclusive
  territories they rent). Leads they get under it are recorded at $0.

Every entry goes through compliance.assert_flat_fee, and the ledger has a
unique key per charge, so running monthly billing twice can't double-bill.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import compliance
from app.domain.types import BillingPlan, ChargeKind, PracticeArea
from app.models import Attorney, Lead, LedgerEntry, Offer, Territory


def period_of(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def _add(db: Session, **entry) -> bool:
    """Insert unless this exact charge already exists. Returns True if inserted."""
    compliance.assert_flat_fee(entry["kind"], entry["amount_cents"])
    exists = db.scalar(
        select(LedgerEntry.id).where(
            LedgerEntry.attorney_id == entry["attorney_id"],
            LedgerEntry.kind == entry["kind"],
            LedgerEntry.period == entry["period"],
            LedgerEntry.reference == entry["reference"],
        )
    )
    if exists:
        return False
    db.add(LedgerEntry(**entry))
    return True


def charge_for_delivery(db: Session, offer: Offer, lead: Lead, now: datetime) -> None:
    kind = ChargeKind.PER_LEAD if offer.price_cents > 0 else ChargeKind.INCLUDED
    _add(
        db,
        attorney_id=offer.attorney_id,
        lead_id=lead.id,
        kind=kind,
        amount_cents=offer.price_cents,
        period=period_of(now),
        reference=f"lead:{lead.id}",
        description=f"{PracticeArea(lead.practice_area).label} lead in {lead.zip_code}",
        created_at=now,
    )


def run_monthly_billing(db: Session, period: str, now: datetime) -> tuple[int, int]:
    created = skipped = 0
    subscribers = db.scalars(
        select(Attorney).where(Attorney.plan == BillingPlan.SUBSCRIPTION, Attorney.active)
    ).all()
    for a in subscribers:
        ok = _add(
            db,
            attorney_id=a.id,
            lead_id=None,
            kind=ChargeKind.SUBSCRIPTION,
            amount_cents=a.monthly_fee_cents,
            period=period,
            reference="plan",
            description=f"Monthly subscription {period}",
            created_at=now,
        )
        if ok:
            created += 1
        else:
            skipped += 1

    for t in db.scalars(select(Territory)).all():
        ok = _add(
            db,
            attorney_id=t.attorney_id,
            lead_id=None,
            kind=ChargeKind.TERRITORY,
            amount_cents=t.monthly_fee_cents,
            period=period,
            reference=f"territory:{t.id}",
            description=(
                f"Exclusive territory {PracticeArea(t.practice_area).label} / {t.zip_code}"
            ),
            created_at=now,
        )
        if ok:
            created += 1
        else:
            skipped += 1

    db.commit()
    return created, skipped


def statement(db: Session, attorney_id: int, period: str) -> list[LedgerEntry]:
    return list(
        db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.attorney_id == attorney_id, LedgerEntry.period == period)
            .order_by(LedgerEntry.id)
        ).all()
    )
