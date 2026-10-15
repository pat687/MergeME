"""Step 3b: decide HOW the lead gets handed out, and to whom, in what order.

Three ways, like three ways of giving out a job:

* Exclusive  - One attorney "rents" this ZIP + practice area. They get it
               automatically, nobody else is asked.
* Shared     - Up to N attorneys are asked at the same time. The first one to
               claim it wins ("speed to lead").
* Ping tree  - An auction. Everyone above the price floor is lined up by how much
               they bid. The top bidder is asked first; if they pass or time runs
               out, the next one is asked, until N attorneys have been asked. The
               market sets the price: each attorney pays one increment above the
               next bid in line (or the floor), never more than their own bid.

N is the number of attorneys the consumer agreed to in the consent text, so no
model ever shares an inquiry with more attorneys than that. For the ping tree the
limit is applied when each attorney is asked (see offers.advance), so someone who
is skipped because they went inactive doesn't use up one of the N places.

This module only *plans*. It returns a list of offers to create and never
writes to the database, which keeps the business rules readable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.matching import Candidate, ineligibility_reason
from app.domain.types import BillingPlan, DistributionModel, OfferStatus


@dataclass(frozen=True)
class OfferSpec:
    attorney_id: int
    round: int
    price_cents: int
    status: OfferStatus


@dataclass(frozen=True)
class DistributionPlan:
    model: DistributionModel
    offers: list[OfferSpec]
    skipped: dict[int, str] = field(default_factory=dict)
    note: str = ""

    @property
    def has_buyer(self) -> bool:
        return bool(self.offers)


def _price(c: Candidate, practice_area: str) -> int:
    """What this attorney pays if they take the lead. Subscribers pay $0 per lead."""
    return 0 if c.plan == BillingPlan.SUBSCRIPTION else c.bid_for(practice_area)


def plan_distribution(
    *,
    practice_area: str,
    zip_code: str,
    state: str,
    territory_owner: Candidate | None,
    candidates: list[Candidate],
    policy_model: DistributionModel,
    max_recipients: int,
    floor_cents: int,
    pricing: str = "second_price",
    increment_cents: int = 100,
) -> DistributionPlan:
    where = {"practice_area": practice_area, "zip_code": zip_code, "state": state}
    skipped: dict[int, str] = {}

    # 1) Exclusive territory wins if its owner can take the lead right now.
    if territory_owner is not None:
        reason = ineligibility_reason(territory_owner, **where)
        if reason is None:
            offer = OfferSpec(territory_owner.attorney_id, 1, 0, OfferStatus.ACCEPTED)
            return DistributionPlan(
                DistributionModel.EXCLUSIVE,
                [offer],
                note=f"exclusive territory owned by {territory_owner.name}",
            )
        skipped[territory_owner.attorney_id] = f"exclusive owner unavailable: {reason}"

    # 2) Otherwise, everyone who qualifies goes into the pool.
    eligible: list[Candidate] = []
    for c in candidates:
        if c.attorney_id in skipped:
            continue
        reason = ineligibility_reason(c, **where)
        if reason:
            skipped[c.attorney_id] = reason
        else:
            eligible.append(c)

    if policy_model == DistributionModel.PING_TREE:
        return _ping_tree(
            eligible, practice_area, floor_cents, max_recipients, skipped, pricing, increment_cents
        )
    return _shared(eligible, practice_area, max_recipients, skipped)


def _shared(
    eligible: list[Candidate], practice_area: str, max_recipients: int, skipped: dict[int, str]
) -> DistributionPlan:
    # Fairness first: attorneys with fewer leads today go to the front,
    # then higher bids, then attorney id so the order is always repeatable.
    ranked = sorted(
        eligible, key=lambda c: (c.accepted_today, -c.bid_for(practice_area), c.attorney_id)
    )
    chosen, overflow = ranked[:max_recipients], ranked[max_recipients:]
    for c in overflow:
        skipped[c.attorney_id] = f"shared pool already full ({max_recipients} attorneys)"
    offers = [
        OfferSpec(c.attorney_id, 1, _price(c, practice_area), OfferStatus.PENDING) for c in chosen
    ]
    return DistributionPlan(
        DistributionModel.SHARED, offers, skipped, f"offered to {len(offers)} attorneys at once"
    )


def market_price(own_bid: int, next_bid: int | None, floor_cents: int, increment_cents: int) -> int:
    """Second-price rule: one increment above the next bid in line (the floor if nobody
    is behind you), never more than your own bid. Bidding what a lead is truly worth to
    you is always safe: you never pay more than it takes to beat the next attorney."""
    market = floor_cents if next_bid is None else max(floor_cents, next_bid + increment_cents)
    return min(own_bid, market)


def _ping_tree(
    eligible: list[Candidate],
    practice_area: str,
    floor_cents: int,
    max_recipients: int,
    skipped: dict[int, str],
    pricing: str,
    increment_cents: int,
) -> DistributionPlan:
    bidders: list[Candidate] = []
    for c in eligible:
        if c.bid_for(practice_area) < floor_cents:
            skipped[c.attorney_id] = f"bid below the ${floor_cents / 100:.2f} floor"
        else:
            bidders.append(c)

    # Highest bid first; ties go to whoever has had fewer leads today.
    # Everyone stays in line; offers.advance stops after max_recipients have been asked.
    ranked = sorted(
        bidders, key=lambda c: (-c.bid_for(practice_area), c.accepted_today, c.attorney_id)
    )
    bids = [c.bid_for(practice_area) for c in ranked]

    def price(position: int, c: Candidate) -> int:
        if c.plan == BillingPlan.SUBSCRIPTION:
            return 0  # subscribers pay a flat monthly fee, not per lead
        if pricing == "first_price":
            return bids[position]
        following = bids[position + 1] if position + 1 < len(bids) else None
        return market_price(bids[position], following, floor_cents, increment_cents)

    offers = [
        OfferSpec(
            c.attorney_id,
            position + 1,
            price(position, c),
            OfferStatus.PENDING if position == 0 else OfferStatus.QUEUED,
        )
        for position, c in enumerate(ranked)
    ]
    asked = min(len(offers), max_recipients)
    rule = "second-price" if pricing == "second_price" else "first-price"
    return DistributionPlan(
        DistributionModel.PING_TREE,
        offers,
        skipped,
        f"{len(offers)} bidders lined up by bid ({rule}); at most {asked} will be asked",
    )
