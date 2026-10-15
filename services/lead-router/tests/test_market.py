"""The lead marketplace: the market sets auction prices, attorneys manage their own bids.

Demo network (app/seed.py): personal injury bids are Avery $95, Jordan $80, Sam $70
(floor $50); Avery also rents personal injury in 60614 exclusively.
"""

from __future__ import annotations

import pytest

from app.domain.distribution import market_price, plan_distribution
from app.domain.matching import Candidate
from app.domain.types import DistributionModel
from app.models import LedgerEntry
from app.services import links
from tests.conftest import ADMIN, SECRET, inquiry_data, token

WHERE = {"practice_area": "personal_injury", "zip_code": "60601", "state": "IL"}


def lawyer(attorney_id: int, bid: int, plan: str = "per_lead") -> Candidate:
    return Candidate(
        attorney_id=attorney_id,
        name=f"Attorney {attorney_id}",
        bar_states=["IL"],
        service_zips=["606"],
        bids_cents={"personal_injury": bid},
        plan=plan,
        daily_cap=10,
        accepted_today=0,
        active=True,
    )


def auction(bidders, pricing="second_price", floor=5000):
    return plan_distribution(
        **WHERE,
        territory_owner=None,
        candidates=bidders,
        policy_model=DistributionModel.PING_TREE,
        max_recipients=3,
        floor_cents=floor,
        pricing=pricing,
        increment_cents=100,
    )


# --- Market price ------------------------------------------------------------
def test_market_price_is_one_step_above_the_next_bid_never_above_your_own():
    assert market_price(9500, 8000, 5000, 100) == 8100
    assert market_price(8000, 8000, 5000, 100) == 8000  # a tie: you pay your own bid
    assert market_price(7000, None, 5000, 100) == 5000  # nobody behind you: the floor
    assert market_price(5200, 3000, 5000, 100) == 5000  # never below the floor


def test_each_bidder_pays_the_market_price_for_their_place_in_line():
    plan = auction([lawyer(1, 9500), lawyer(2, 8000), lawyer(3, 7000)])
    assert [(o.attorney_id, o.price_cents) for o in plan.offers] == [
        (1, 8100),
        (2, 7100),
        (3, 5000),
    ]
    assert "second-price" in plan.note


def test_first_price_mode_charges_each_bidder_their_own_bid():
    plan = auction([lawyer(1, 9500), lawyer(2, 8000)], pricing="first_price")
    assert [o.price_cents for o in plan.offers] == [9500, 8000]


def test_subscribers_still_pay_nothing_per_lead_but_their_bid_sets_the_price_above_them():
    plan = auction([lawyer(1, 9500), lawyer(2, 9000, plan="subscription")])
    assert [o.price_cents for o in plan.offers] == [9100, 0]


# --- Attorneys manage their own bids ----------------------------------------
def attorney_id(client, name: str) -> int:
    return next(
        a["id"]
        for a in client.get("/api/v1/admin/attorneys", headers=ADMIN).json()
        if a["name"] == name
    )


def bids_headers(attorney: int) -> dict:
    return {"X-Attorney-Token": links.sign_bids(SECRET, attorney)}


def board(client, attorney: int) -> dict[str, dict]:
    r = client.get(f"/api/v1/bids/{attorney}", headers=bids_headers(attorney))
    assert r.status_code == 200, r.text
    return {row["practice_area"]: row for row in r.json()["areas"]}


def test_attorney_sees_rank_bid_to_be_first_and_floor(client):
    jordan = attorney_id(client, "Jordan Reyes")
    pi = board(client, jordan)["personal_injury"]
    assert (pi["my_bid_cents"], pi["rank"], pi["competing_bidders"]) == (8000, 2, 2)
    assert (pi["to_be_first_cents"], pi["floor_cents"], pi["auctioned"]) == (9600, 5000, True)
    family = board(client, jordan)["family_law"]
    assert (family["my_bid_cents"], family["rank"], family["auctioned"]) == (None, None, False)


def test_raising_a_bid_moves_the_attorney_up_and_the_next_lead_uses_it(client, db):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.put(
        f"/api/v1/bids/{jordan}",
        json={"bids_cents": {"personal_injury": 9900}},
        headers=bids_headers(jordan),
    )
    assert r.status_code == 200, r.text
    pi = {row["practice_area"]: row for row in r.json()["areas"]}["personal_injury"]
    assert (pi["my_bid_cents"], pi["rank"]) == (9900, 1)

    created = client.post("/api/v1/intake", json=inquiry_data()).json()
    detail = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
    first = next(o for o in detail["offers"] if o["round"] == 1)
    assert (first["attorney_id"], first["price_cents"]) == (jordan, 9600)  # Avery bid $95

    accepted = client.post(
        f"/api/v1/offers/{first['id']}/accept", json={"token": token(first["id"])}
    )
    assert accepted.status_code == 200
    assert "Market price" in accepted.json()["price_note"]
    charge = db.query(LedgerEntry).filter_by(lead_id=created["lead_id"]).one()
    assert charge.amount_cents == 9600


@pytest.mark.parametrize(
    ("bids", "message"),
    [
        ({"personal_injury": 1000}, "lowest bid accepted is $50.00"),
        ({"personal_injury": 900_000}, "between $0 and $5,000.00"),
        ({"personal_injury": None, "workers_comp": None}, "keep at least one practice area"),
    ],
)
def test_bad_bids_are_explained_and_nothing_is_saved(client, bids, message):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.put(
        f"/api/v1/bids/{jordan}", json={"bids_cents": bids}, headers=bids_headers(jordan)
    )
    assert r.status_code == 422 and message in r.json()["detail"]
    assert board(client, jordan)["personal_injury"]["my_bid_cents"] == 8000


def test_an_exclusive_territory_area_cannot_be_dropped_from_the_bid_page(client):
    avery = attorney_id(client, "Avery Stone")
    r = client.put(
        f"/api/v1/bids/{avery}",
        json={"bids_cents": {"personal_injury": None}},
        headers=bids_headers(avery),
    )
    assert r.status_code == 422 and "exclusive territory" in r.json()["detail"]
    assert board(client, avery)["personal_injury"]["exclusive_territory"] is True


def test_leaving_a_bid_empty_stops_leads_in_that_area(client):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.put(
        f"/api/v1/bids/{jordan}",
        json={"bids_cents": {"workers_comp": None, "employment": 4500}},
        headers=bids_headers(jordan),
    )
    assert r.status_code == 200, r.text
    rows = {row["practice_area"]: row for row in r.json()["areas"]}
    assert (rows["workers_comp"]["my_bid_cents"], rows["employment"]["my_bid_cents"]) == (
        None,
        4500,
    )


@pytest.mark.parametrize(
    "bad",
    [None, "nope", "ééé", "offer-token"],
)
def test_bid_pages_need_their_own_signed_link(client, bad):
    jordan = attorney_id(client, "Jordan Reyes")
    if bad == "offer-token":
        bad = links.sign_offer(SECRET, str(jordan))  # an offer token is not a bid token
    headers = {} if bad is None else {"X-Attorney-Token": bad.encode("utf-8")}
    assert client.get(f"/api/v1/bids/{jordan}", headers=headers).status_code == 404
    assert client.get("/api/v1/bids/9999", headers=bids_headers(9999)).status_code == 404


# --- Admin --------------------------------------------------------------------
def test_admin_emails_the_bid_link_and_sees_the_market(client, mailbox):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.post(f"/api/v1/admin/attorneys/{jordan}/bid-link", headers=ADMIN)
    assert r.status_code == 200 and f"/bids/{jordan}#t=" in r.json()["link"]  # demo mode
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    email = next(m for m in mailbox if m["subject"] == "Manage your lead bids")
    assert r.json()["link"] in email["body"]

    market = {
        m["practice_area"]: m for m in client.get("/api/v1/admin/market", headers=ADMIN).json()
    }
    pi = market["personal_injury"]
    assert [b["bid_cents"] for b in pi["bidders"]] == [9500, 8000, 7000]
    assert (pi["model"], pi["floor_cents"], pi["sold_last_30_days"]) == ("ping_tree", 5000, 0)


def test_offer_emails_explain_the_price_and_link_to_the_bid_page(client, mailbox):
    client.post("/api/v1/intake", json=inquiry_data())
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    offer = next(m for m in mailbox if m["to"] == "avery.stone@example.com")
    assert "Market price" in offer["body"] and "/bids/" in offer["body"]


def test_the_bids_page_is_served(client):
    assert "Your lead bids" in client.get("/bids/1").text
