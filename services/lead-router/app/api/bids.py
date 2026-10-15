"""Attorneys manage their own bids. The signed link in their bid email is the only credential."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings
from app.config import Settings
from app.db import utcnow
from app.domain.types import BillingPlan, DistributionModel
from app.models import Attorney, Territory
from app.schemas import BidsUpdate, BidsView
from app.services import links, market

router = APIRouter(prefix="/api/v1/bids", tags=["attorney bids"])

TOKEN = Header(default=None, description="Signed token from the bid link (after '#t=')")


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _attorney(db: Session, settings: Settings, attorney_id: int, token: str | None) -> Attorney:
    # Same answer for "no such attorney" and "bad link" so links can't be probed.
    if not links.verify_bids(settings.link_signing_secret, attorney_id, token or ""):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bid page not found")
    attorney = db.get(Attorney, attorney_id)
    if attorney is None:
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
    rule = (
        f"In auctioned practice areas the highest bidder is asked first; if you take the lead, "
        f"{auction}. In other areas your bid is your flat price per lead. You only pay for "
        "leads you accept, and the price never depends on whether you're hired."
    )
    if plan == BillingPlan.SUBSCRIPTION:
        rule += " On your subscription, leads are included: your bids only set your place in line."
    return rule


def _view(db: Session, settings: Settings, attorney: Attorney) -> BidsView:
    return BidsView(
        attorney_id=attorney.id,
        name=attorney.name,
        firm=attorney.firm,
        plan=attorney.plan,
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
    _attorney(db, settings, attorney_id, x_attorney_token)
    # Fresh and locked, so a bid change and an admin edit at the same moment can't
    # silently overwrite each other.
    attorney = db.scalars(
        select(Attorney)
        .where(Attorney.id == attorney_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    owned = {
        t.practice_area
        for t in db.scalars(select(Territory).where(Territory.attorney_id == attorney.id))
    }
    bids = dict(attorney.bids_cents)
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
        floor = settings.floor_for(area)
        if not 0 <= cents <= settings.max_bid_cents:
            problems.append(
                f"{name}: bids must be between $0 and {_dollars(settings.max_bid_cents)}"
            )
        elif settings.policy_for(area) == DistributionModel.PING_TREE and cents < floor:
            problems.append(f"{name}: the lowest bid accepted is {_dollars(floor)}")
        bids[area.value] = cents
    if not bids:
        problems.append("keep at least one practice area, or ask us to pause your account")
    if problems:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(problems))
    attorney.bids_cents = bids
    db.commit()
    return _view(db, settings, attorney)
