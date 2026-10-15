"""The assembly line. One inquiry goes in; a routed (or politely refused) lead comes out.

    1. Capture   save what the person typed, plus proof of consent
    2. Scrub     throw out spam, duplicates and junk          (domain/scrubbing.py)
    3. Score     grade the lead 0-100 and explain why          (domain/scoring.py)
    4. Match     find attorneys allowed to take it             (domain/matching.py)
    5. Route     exclusive, shared or ping tree                (domain/distribution.py)
    6. Notify    queue emails and the eXo sync                 (services/outbox.py)

Everything happens in one database transaction: either the whole step list
is saved, or none of it is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain import compliance, scoring, scrubbing
from app.domain.distribution import DistributionPlan, plan_distribution
from app.domain.matching import Candidate
from app.domain.types import DistributionModel, LeadStatus, OfferStatus
from app.models import Attorney, Lead, Offer, Territory
from app.schemas import IntakeRequest
from app.services import audit, emails, exo_sync, offers, outbox


@dataclass(frozen=True)
class RequestMeta:
    ip_address: str | None = None
    user_agent: str | None = None


def _lock_key(value: str) -> int:
    """A stable signed 64-bit number for pg_advisory_xact_lock."""
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


def _serialize_duplicate_check(db: Session, lead: Lead) -> None:
    """Make two identical submissions arriving together take turns.

    Without this, both could run the duplicate check before either is saved, and
    the same person would be sold twice. Postgres advisory locks are held until the
    transaction ends; keys are taken in sorted order so two requests can't deadlock.
    SQLite already allows only one writer at a time.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    keys = sorted(
        {
            _lock_key(f"email:{lead.email}|{lead.practice_area}"),
            _lock_key(f"phone:{lead.phone}|{lead.practice_area}"),
        }
    )
    for key in keys:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _is_duplicate(db: Session, lead: Lead, now: datetime, window_days: int) -> bool:
    """Same email or phone, same practice area, recently, and not itself junk."""
    since = now - timedelta(days=window_days)
    return bool(
        db.scalar(
            select(Lead.id)
            .where(
                Lead.id != lead.id,
                or_(Lead.email == lead.email, Lead.phone == lead.phone),
                Lead.practice_area == lead.practice_area,
                Lead.created_at >= since,
                Lead.status != LeadStatus.REJECTED,
            )
            .limit(1)
        )
    )


def _snapshot(attorney: Attorney, accepted_today: dict[int, int]) -> Candidate:
    return Candidate(
        attorney_id=attorney.id,
        name=attorney.name,
        active=attorney.active,
        bar_states=attorney.bar_states,
        service_zips=attorney.service_zips,
        bids_cents=attorney.bids_cents,
        plan=attorney.plan,
        daily_cap=attorney.daily_cap,
        accepted_today=accepted_today.get(attorney.id, 0),
    )


def _plan(
    db: Session, settings: Settings, inquiry: IntakeRequest, now: datetime
) -> DistributionPlan:
    territory = db.scalar(
        select(Territory).where(
            Territory.practice_area == inquiry.practice_area,
            Territory.zip_code == inquiry.zip_code,
        )
    )
    if territory is not None:
        # Exclusive leads are assigned on the spot, so lock the owner before counting
        # their leads today: two intakes at once can't both slip under the daily cap.
        db.execute(
            select(Attorney.id).where(Attorney.id == territory.attorney_id).with_for_update()
        )
    counts = offers.accepted_today(db, settings, now)
    # Fine at hundreds of attorneys. At larger scale, pre-filter by state/ZIP in SQL.
    candidates = [_snapshot(a, counts) for a in db.scalars(select(Attorney)).all()]
    owner = next(
        (c for c in candidates if territory and c.attorney_id == territory.attorney_id), None
    )
    return plan_distribution(
        practice_area=inquiry.practice_area,
        zip_code=inquiry.zip_code,
        state=inquiry.state,
        territory_owner=owner,
        candidates=candidates,
        policy_model=DistributionModel(settings.policy_for(inquiry.practice_area)),
        max_recipients=settings.shared_max_attorneys,
        floor_cents=settings.floor_for(inquiry.practice_area),
    )


def process_intake(
    db: Session, settings: Settings, inquiry: IntakeRequest, meta: RequestMeta, now: datetime
) -> Lead:
    # 1) Capture -----------------------------------------------------------
    lead = Lead(
        created_at=now,
        status=LeadStatus.RECEIVED,
        consent_at=now,
        consent_text=compliance.consent_text(settings.platform_name, settings.shared_max_attorneys),
        ip_address=meta.ip_address,
        user_agent=(meta.user_agent or "")[:300] or None,
        **inquiry.model_dump(exclude={"website"}),
    )
    db.add(lead)
    db.flush()
    audit.record(db, lead.id, "received", practice_area=lead.practice_area, zip_code=lead.zip_code)

    # 2) Scrub ---------------------------------------------------------------
    _serialize_duplicate_check(db, lead)
    duplicate = _is_duplicate(db, lead, now, settings.duplicate_window_days)
    checked = scrubbing.scrub(
        inquiry, is_duplicate=duplicate, consent_version=settings.disclosure_version
    )
    if not checked.ok:
        lead.status = LeadStatus.REJECTED
        lead.rejection_reasons = checked.reasons
        audit.record(db, lead.id, "rejected", reasons=checked.reasons)
        db.commit()
        return lead
    audit.record(db, lead.id, "scrubbed", result="all checks passed")

    # 3) Score ---------------------------------------------------------------
    graded = scoring.score(inquiry, now.date())
    lead.score, lead.tier = graded.total, graded.tier
    lead.score_breakdown, lead.flags = graded.breakdown(), graded.flags
    audit.record(db, lead.id, "scored", score=graded.total, tier=graded.tier, flags=graded.flags)

    # 4 + 5) Match and route ------------------------------------------------
    plan = _plan(db, settings, inquiry, now)
    names = {a.id: a.name for a in db.scalars(select(Attorney)).all()}
    audit.record(
        db,
        lead.id,
        "routing_planned",
        model=plan.model,
        note=plan.note,
        line_up=[names[o.attorney_id] for o in plan.offers],
        skipped={names[k]: v for k, v in plan.skipped.items()},
    )

    if not plan.has_buyer:
        lead.status = LeadStatus.UNSERVICEABLE
        audit.record(db, lead.id, "unserviceable", reason="no eligible attorney for this lead")
        outbox.enqueue(db, "email", emails.consumer_receipt(settings, lead))
        db.commit()
        return lead

    lead.distribution_model = plan.model
    if settings.exo_sync_enabled:
        outbox.enqueue(db, "exo.create_lead", exo_sync.lead_payload(lead))

    created = _create_offers(db, settings, lead, plan, now)

    # 6) Notify --------------------------------------------------------------
    if plan.model == DistributionModel.EXCLUSIVE:
        winner = created[0]
        lead.status = LeadStatus.ASSIGNED
        lead.assigned_attorney_id = winner.attorney_id
        lead.assigned_at = now
        offers.finalize_assignment(db, settings, lead, winner, now)
    else:
        lead.status = LeadStatus.OFFERED
        for offer in created:
            if offer.status == OfferStatus.PENDING:
                offers.notify_offer(db, settings, lead, offer)

    outbox.enqueue(db, "email", emails.consumer_receipt(settings, lead))
    db.commit()
    return lead


def _create_offers(
    db: Session, settings: Settings, lead: Lead, plan: DistributionPlan, now: datetime
) -> list[Offer]:
    ttl = offers.ttl_for(settings, plan.model)
    created: list[Offer] = []
    for spec in plan.offers:
        offer = Offer(
            lead_id=lead.id,
            attorney_id=spec.attorney_id,
            round=spec.round,
            status=spec.status,
            price_cents=spec.price_cents,
            created_at=now,
        )
        if spec.status == OfferStatus.PENDING:
            offer.activated_at, offer.expires_at = now, now + ttl
        elif spec.status == OfferStatus.ACCEPTED:
            offer.activated_at = offer.responded_at = now
        db.add(offer)
        created.append(offer)
    db.flush()
    return created
