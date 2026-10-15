"""Attorney endpoints. The signed link in the offer email is the only credential needed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings
from app.config import Settings
from app.db import utcnow
from app.schemas import OfferAction, OfferView
from app.services import offers

router = APIRouter(prefix="/api/v1/offers", tags=["attorney offers"])


def _run(fn, *args):
    try:
        return fn(*args)
    except offers.OfferError as err:
        raise HTTPException(err.status_code, err.message) from err


@router.get(
    "/{offer_id}", response_model=OfferView, summary="See an offer (contact hidden until accepted)"
)
def view_offer(
    offer_id: str,
    t: str = Query(description="Signed token from the offer link"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OfferView:
    return _run(offers.view, db, settings, offer_id, t, utcnow())


@router.post("/{offer_id}/accept", response_model=OfferView, summary="Claim the lead")
def accept_offer(
    offer_id: str,
    body: OfferAction,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OfferView:
    _run(offers.accept, db, settings, offer_id, body.token, utcnow())
    return offers.view(db, settings, offer_id, body.token, utcnow())


@router.post("/{offer_id}/decline", response_model=OfferView, summary="Pass on the lead")
def decline_offer(
    offer_id: str,
    body: OfferAction,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OfferView:
    _run(offers.decline, db, settings, offer_id, body.token, body.reason, utcnow())
    return offers.view(db, settings, offer_id, body.token, utcnow())
