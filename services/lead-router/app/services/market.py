"""The lead marketplace: what each attorney bids, where they rank, and what leads sell for.

Attorneys set their own bids per practice area. For auctioned areas (the ping tree)
the market sets the price: the winner pays one increment above the next bid in line,
never more than their own bid. This module answers the questions an attorney asks
before changing a bid: "where do I rank?", "what would it take to be first?" and
"what do leads actually sell for?".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.types import DistributionModel, OfferStatus, PracticeArea
from app.models import Attorney, Lead, Offer, Territory

RECENT_DAYS = 30


def zips_overlap(mine: list[str], theirs: list[str]) -> bool:
    """'606' (a ZIP prefix) overlaps '60614' and '606'; '606' and '607' don't."""
    return any(a.startswith(b) or b.startswith(a) for a in mine for b in theirs)


def competes_with(me: Attorney, other: Attorney) -> bool:
    """Could these two attorneys be lined up for the same lead?"""
    return (
        other.id != me.id
        and other.active
        and bool(set(me.bar_states) & set(other.bar_states))
        and zips_overlap(me.service_zips, other.service_zips)
    )


def recent_sales(db: Session, now: datetime) -> dict[str, tuple[int, int]]:
    """Per practice area over the last 30 days: (leads sold for a price, average price)."""
    rows = db.execute(
        select(Lead.practice_area, func.count(), func.avg(Offer.price_cents))
        .join(Offer, Offer.lead_id == Lead.id)
        .where(
            Offer.status == OfferStatus.ACCEPTED,
            Offer.price_cents > 0,
            Offer.responded_at >= now - timedelta(days=RECENT_DAYS),
        )
        .group_by(Lead.practice_area)
    ).all()
    return {area: (count, round(avg or 0)) for area, count, avg in rows}


def bid_board(db: Session, settings: Settings, me: Attorney, now: datetime) -> list[dict]:
    """One row per practice area: my bid, my rank among competing bidders, the bid it
    would take to be first, the floor, and what leads have sold for lately."""
    others = [a for a in db.scalars(select(Attorney)).all() if competes_with(me, a)]
    owned = {
        t.practice_area for t in db.scalars(select(Territory).where(Territory.attorney_id == me.id))
    }
    sales = recent_sales(db, now)
    rows = []
    for area in PracticeArea:
        mine = me.bids_cents.get(area)
        rival_bids = sorted(
            (a.bids_cents[area] for a in others if area in a.bids_cents), reverse=True
        )
        floor = settings.floor_for(area)
        auctioned = settings.policy_for(area) == DistributionModel.PING_TREE
        top_rival = rival_bids[0] if rival_bids else None
        to_be_first = (
            floor if top_rival is None else max(floor, top_rival + settings.bid_increment_cents)
        )
        sold, average = sales.get(area, (0, 0))
        rows.append(
            {
                "practice_area": area,
                "label": PracticeArea(area).label,
                "my_bid_cents": mine,
                "auctioned": auctioned,
                "floor_cents": floor if auctioned else 0,
                "competing_bidders": len(rival_bids),
                # Rank and "bid to be first" only mean something where leads are auctioned.
                "rank": 1 + sum(b > mine for b in rival_bids) if auctioned and mine else None,
                "tied": bool(auctioned and mine is not None and mine in rival_bids),
                "to_be_first_cents": to_be_first if auctioned else None,
                "sold_last_30_days": sold,
                "average_price_cents": average or None,
                "exclusive_territory": area in owned,
            }
        )
    return rows


def overview(db: Session, settings: Settings, now: datetime) -> list[dict]:
    """The whole market, for the operations dashboard."""
    attorneys = db.scalars(select(Attorney).where(Attorney.active)).all()
    sales = recent_sales(db, now)
    rows = []
    for area in PracticeArea:
        bids = sorted(
            (
                {"attorney_id": a.id, "name": a.name, "bid_cents": a.bids_cents[area]}
                for a in attorneys
                if area in a.bids_cents
            ),
            key=lambda b: -b["bid_cents"],
        )
        sold, average = sales.get(area, (0, 0))
        rows.append(
            {
                "practice_area": area,
                "label": PracticeArea(area).label,
                "model": settings.policy_for(area),
                "floor_cents": settings.floor_for(area),
                "bidders": bids,
                "sold_last_30_days": sold,
                "average_price_cents": average or None,
            }
        )
    return rows


def price_note(settings: Settings, model: str | None, price_cents: int) -> str | None:
    """One line telling an attorney how this offer's price was set."""
    if price_cents == 0:
        return "Included in your plan."
    if model == DistributionModel.PING_TREE:
        if settings.auction_pricing == "second_price":
            step = f"${settings.bid_increment_cents / 100:,.2f}"
            return (
                f"Market price: {step} more than the next bid in line (or the lowest bid "
                "accepted), never more than your own bid."
            )
        return "Your bid."
    if model == DistributionModel.SHARED:
        return "Your listed price for this practice area."
    return None
