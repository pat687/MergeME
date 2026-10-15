"""The lead marketplace: what each attorney bids, where they rank, and what leads sell for.

Attorneys set their own bids per practice area. For auctioned areas (the ping tree)
the market sets the price: the winner pays one increment above the next bid in line,
never more than their own bid. This module answers the questions an attorney asks
before changing a bid: "where do I rank?", "what would it take to be first?" and
"what do leads actually sell for?".

Rival bids stay private. An attorney sees their place in line and a bid that would
put them first, rounded up to a whole $10, never another attorney's exact bid. That
stops a rival from reading the leader's bid and bidding just under it to push the
leader's price up.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.types import BillingPlan, DistributionModel, OfferStatus, PracticeArea, PriceBasis
from app.models import Attorney, Lead, Offer, Territory

RECENT_DAYS = 30
# "Bid to be first" is rounded up to this step, so it never reveals a rival's exact bid.
FIRST_PLACE_STEP_CENTS = 1000


def round_up(cents: int, step: int = FIRST_PLACE_STEP_CENTS) -> int:
    return -(-cents // step) * step


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


def _pays(attorney: Attorney) -> bool:
    return attorney.plan != BillingPlan.SUBSCRIPTION


def bid_board(db: Session, settings: Settings, me: Attorney, now: datetime) -> list[dict]:
    """One row per practice area: my bid, my place in line, a bid that would put me first,
    the lowest bid accepted, and what leads have sold for lately."""
    others = [a for a in db.scalars(select(Attorney)).all() if competes_with(me, a)]
    owned = {
        t.practice_area for t in db.scalars(select(Territory).where(Territory.attorney_id == me.id))
    }
    sales = recent_sales(db, now)
    rows = []
    for area in PracticeArea:
        mine = me.bids_cents.get(area)
        floor = settings.floor_for(area)
        auctioned = settings.policy_for(area) == DistributionModel.PING_TREE
        # Only paying bids at or above the floor are in line. Subscribers are asked after
        # every paying bidder (domain/distribution.py), so they don't compete on bids.
        rivals = [
            a.bids_cents[area] for a in others if _pays(a) and a.bids_cents.get(area, -1) >= floor
        ]
        below_floor = _pays(me) and mine is not None and mine < floor
        in_line = auctioned and _pays(me) and mine is not None and not below_floor
        # A rival with the same bid may be asked first (fewer leads today), so a tie
        # counts against me. Rank and "bid to be first" only mean something in auctions.
        rank = 1 + sum(b >= mine for b in rivals) if in_line else None
        to_be_first = None
        if auctioned and _pays(me) and rank != 1:
            top = max(rivals, default=None)
            step = max(settings.bid_increment_cents, 1)  # first means strictly above the top
            to_be_first = floor if top is None else max(floor, round_up(top + step))
            if to_be_first > settings.max_bid_cents:
                to_be_first = None  # no bid the page accepts would get there
        sold, average = sales.get(area, (0, 0))
        rows.append(
            {
                "practice_area": area,
                "label": PracticeArea(area).label,
                "my_bid_cents": mine,
                "auctioned": auctioned,
                "floor_cents": floor,
                "below_floor": below_floor,
                "competing_bidders": len(rivals),
                "rank": rank,
                "to_be_first_cents": to_be_first,
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
                {
                    "attorney_id": a.id,
                    "name": a.name,
                    "bid_cents": a.bids_cents[area],
                    "plan": a.plan,
                }
                for a in attorneys
                if area in a.bids_cents
            ),
            # In line order: paying bidders by bid, then subscribers (who don't bid).
            key=lambda b: (b["plan"] == BillingPlan.SUBSCRIPTION, -b["bid_cents"]),
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


def price_note(basis: str | None, increment_cents: int) -> str | None:
    """One line telling an attorney how an offer's price was set. Written once, when the
    offer is priced (pipeline._create_offers), and saved with it, so a later change to
    the pricing settings can't make an old offer describe a rule that wasn't used."""
    match basis:
        case PriceBasis.TERRITORY:
            return "Included in the rent for your exclusive territory."
        case PriceBasis.SUBSCRIPTION:
            return "Included in your plan."
        case PriceBasis.SET_PRICE:
            return "Your set price for this practice area."
        case PriceBasis.FIRST_PRICE:
            return "Your bid."
        case PriceBasis.SECOND_PRICE:
            return (
                f"Market price: ${increment_cents / 100:,.2f} more than the next bid in line "
                "(or the lowest bid accepted), never more than your own bid."
            )
    return None
