"""Attorneys manage their own bids. The signed link in their bid email is the only credential.

Only the newest link works (sending a new one turns off the old), and only while the
attorney is active. Subscribers can see the page but not change it: their leads are
included in a flat fee, so their practice areas are set with staff.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings
from app.config import Settings
from app.db import utcnow
from app.domain.types import BillingPlan
from app.models import Attorney, BidChange, Territory
from app.schemas import BidsUpdate, BidsView
from app.services import links, market

router = APIRouter(prefix="/api/v1/bids", tags=["attorney bids"])

TOKEN = Header(default=None, description="Signed token from the bid link (after '#t=')")


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _attorney(db: Session, settings: Settings, attorney_id: int, token: str | None) -> Attorney:
    # One answer for "no such attorney", "inactive", "old link" and "bad link", so links
    # can't be probed.
    attorney = db.get(Attorney, attorney_id)
    if (
        attorney is None
        or not attorney.active
        or not links.verify_bids(
            settings.link_signing_secret, attorney.id, attorney.bids_link_version, token or ""
        )
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bid page not found")
    return attorney


def pricing_rule(settings: Settings, plan: str) -> str:
    increment = _dollars(settings.bid_increment_cents)
    auction = (
        f"you pay {increment} more than the next bid in line (or the lowest bid accepted), "
        "never more than your own bid"
        if settings.auction_pricing == "second_price"
        else "you pay your own bid"
    )
    if plan == BillingPlan.SUBSCRIPTION:
        return (
            "Your leads are included in your subscription, so you don't bid. In auctioned "
            "practice areas you're asked after attorneys who pay per lead. To change your "
            "practice areas, contact us."
        )
    return (
        f"In auctioned practice areas the highest bidder is asked first; if you take the lead, "
        f"{auction}. In other areas your bid is your flat price per lead. You only pay for "
        "leads you accept, and the price never depends on whether you're hired. We don't "
        "show other attorneys' bids, only your place in line."
    )


def _view(db: Session, settings: Settings, attorney: Attorney) -> BidsView:
    return BidsView(
        attorney_id=attorney.id,
        name=attorney.name,
        firm=attorney.firm,
        plan=attorney.plan,
        can_edit=attorney.plan != BillingPlan.SUBSCRIPTION,
        pricing_rule=pricing_rule(settings, attorney.plan),
        increment_cents=settings.bid_increment_cents,
        max_bid_cents=settings.max_bid_cents,
        areas=market.bid_board(db, settings, attorney, utcnow()),
    )


@router.get("/{attorney_id}", response_model=BidsView, summary="See your bids and the market")
def my_bids(
    attorney_id: int,
    x_attorney_token: str | None = TOKEN,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BidsView:
    return _view(db, settings, _attorney(db, settings, attorney_id, x_attorney_token))


@router.put("/{attorney_id}", response_model=BidsView, summary="Change your bids")
def update_bids(
    attorney_id: int,
    body: BidsUpdate,
    x_attorney_token: str | None = TOKEN,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BidsView:
    if _attorney(db, settings, attorney_id, x_attorney_token).plan == BillingPlan.SUBSCRIPTION:
        # A bid costs a subscriber nothing, so letting them set one would let them buy
        # first place for free.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "your leads are included in your subscription; contact us to change your areas",
        )
    # Fresh and locked, so a bid change and an admin edit at the same moment can't
    # silently overwrite each other.
    attorney = db.scalars(
        select(Attorney)
        .where(Attorney.id == attorney_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    now = utcnow()
    saves = db.scalar(
        select(func.count())
        .select_from(BidChange)
        .where(BidChange.attorney_id == attorney.id, BidChange.at > now - timedelta(days=1))
    )
    if saves >= settings.max_bid_saves_per_day:
        db.rollback()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"you can save bid changes {settings.max_bid_saves_per_day} times a day; "
            "please try again tomorrow or contact us",
        )
    owned = {
        t.practice_area
        for t in db.scalars(select(Territory).where(Territory.attorney_id == attorney.id))
    }
    before = dict(attorney.bids_cents)
    bids = dict(before)
    problems = []
    for area, cents in body.bids_cents.items():
        name = area.label
        if cents is None:
            if area.value in owned:
                problems.append(
                    f"{name}: you rent an exclusive territory here; contact us to change it"
                )
            bids.pop(area.value, None)
            continue
        # The floor applies everywhere: in shared areas the bid is the price itself.
        floor = settings.floor_for(area)
        if cents < floor:
            problems.append(f"{name}: the lowest bid accepted is {_dollars(floor)}")
        elif cents > settings.max_bid_cents:
            problems.append(
                f"{name}: the highest bid accepted is {_dollars(settings.max_bid_cents)}"
            )
        bids[area.value] = cents
    if not bids:
        problems.append("keep at least one practice area, or ask us to pause your account")
    if problems:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(problems))
    changed = {
        area: [before.get(area), bids.get(area)]
        for area in sorted(set(before) | set(bids))
        if before.get(area) != bids.get(area)
    }
    if changed:  # a save that changes nothing doesn't use up one of the day's saves
        attorney.bids_cents = bids
        db.add(BidChange(attorney_id=attorney.id, at=now, changes=changed))
    db.commit()
    return _view(db, settings, attorney)
