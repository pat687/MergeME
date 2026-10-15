"""The lead marketplace: the market sets auction prices, attorneys manage their own bids.

Demo network (app/seed.py): personal injury bids are Avery $95, Jordan $80, Sam $70
(floor $50); Avery also rents personal injury in 60614 exclusively.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.distribution import market_price, plan_distribution
from app.domain.matching import Candidate
from app.domain.types import DistributionModel, PriceBasis
from app.models import LedgerEntry
from app.services import links
from tests.conftest import ADMIN, SECRET, inquiry_data, token

WHERE = {"practice_area": "personal_injury", "zip_code": "60601", "state": "IL"}


def lawyer(attorney_id: int, bid: int, plan: str = "pay_per_lead") -> Candidate:
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


def auction(bidders, pricing="second_price", floor=5000, model=DistributionModel.PING_TREE):
    return plan_distribution(
        **WHERE,
        territory_owner=None,
        candidates=bidders,
        policy_model=model,
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
    assert {o.basis for o in plan.offers} == {PriceBasis.SECOND_PRICE}
    assert "second-price" in plan.note


def test_first_price_mode_charges_each_bidder_their_own_bid():
    plan = auction([lawyer(1, 9500), lawyer(2, 8000)], pricing="first_price")
    assert [o.price_cents for o in plan.offers] == [9500, 8000]
    assert {o.basis for o in plan.offers} == {PriceBasis.FIRST_PRICE}


def test_a_subscribers_free_bid_buys_no_place_in_line_and_sets_no_price():
    # Morgan-style subscriber bids far above everyone: still asked last, at $0, and the
    # paying attorney in front pays as if the subscriber weren't there.
    plan = auction([lawyer(1, 9500), lawyer(2, 500_000, plan="subscription"), lawyer(3, 8000)])
    assert [(o.attorney_id, o.price_cents, o.basis) for o in plan.offers] == [
        (1, 8100, PriceBasis.SECOND_PRICE),
        (3, 5000, PriceBasis.SECOND_PRICE),
        (2, 0, PriceBasis.SUBSCRIPTION),
    ]
    assert "then 1 subscriber;" in plan.note


def test_shared_prices_below_the_minimum_are_left_out():
    plan = auction(
        [lawyer(1, 0), lawyer(2, 2000), lawyer(3, 0, plan="subscription")],
        floor=1500,
        model=DistributionModel.SHARED,
    )
    assert [(o.attorney_id, o.price_cents, o.basis) for o in plan.offers] == [
        (2, 2000, PriceBasis.SET_PRICE),
        (3, 0, PriceBasis.SUBSCRIPTION),
    ]
    assert plan.skipped[1] == "price below the $15.00 minimum"


def test_a_subscribers_stored_number_never_wins_a_shared_place():
    payer_a, payer_b = lawyer(1, 4000), lawyer(2, 3500)
    subscriber = lawyer(3, 500_000, plan="subscription")
    plan = auction([payer_a, payer_b, subscriber], floor=1500, model=DistributionModel.SHARED)
    assert [o.attorney_id for o in plan.offers] == [1, 2, 3]  # room for all three
    plan = plan_distribution(
        **WHERE,
        territory_owner=None,
        candidates=[payer_a, payer_b, subscriber],
        policy_model=DistributionModel.SHARED,
        max_recipients=2,
        floor_cents=1500,
    )
    assert [o.attorney_id for o in plan.offers] == [1, 2]
    assert "pool already full" in plan.skipped[3]

    # Fairness still comes first: a subscriber with fewer leads today goes ahead.
    busy = [replace(payer_a, accepted_today=1), replace(payer_b, accepted_today=1)]
    plan = plan_distribution(
        **WHERE,
        territory_owner=None,
        candidates=[*busy, subscriber],
        policy_model=DistributionModel.SHARED,
        max_recipients=2,
        floor_cents=1500,
    )
    assert [o.attorney_id for o in plan.offers] == [3, 1]


def test_a_floor_above_the_highest_bid_is_refused_at_startup():
    with pytest.raises(ValidationError, match="nobody could bid there"):
        Settings(_env_file=None, max_bid_cents=7000)  # med-mal's floor is $75


@pytest.mark.parametrize(
    "floors",
    [{"personal_injury": 5000}, {"personal_injury": 5000, "*": 0}, {"*": 1500, "bankruptcy": 0}],
)
def test_every_practice_area_needs_a_floor_above_zero(floors):
    # The last bidder in an auction pays the floor, so a missing floor would give leads away.
    with pytest.raises(ValidationError, match="ROUTER_FLOOR_PRICE_CENTS"):
        Settings(_env_file=None, floor_price_cents=floors)


# --- Attorneys manage their own bids ----------------------------------------
def attorney_id(client, name: str) -> int:
    return next(
        a["id"]
        for a in client.get("/api/v1/admin/attorneys", headers=ADMIN).json()
        if a["name"] == name
    )


def bids_headers(attorney: int, version: int = 0) -> dict:
    return {"X-Attorney-Token": links.sign_bids(SECRET, attorney, version)}


def bid_page(client, attorney: int) -> dict:
    r = client.get(f"/api/v1/bids/{attorney}", headers=bids_headers(attorney))
    assert r.status_code == 200, r.text
    return r.json()


def board(client, attorney: int) -> dict[str, dict]:
    return {row["practice_area"]: row for row in bid_page(client, attorney)["areas"]}


def test_attorney_sees_place_in_line_and_a_rounded_bid_to_be_first(client):
    jordan = attorney_id(client, "Jordan Reyes")
    pi = board(client, jordan)["personal_injury"]
    assert (pi["my_bid_cents"], pi["rank"], pi["competing_bidders"]) == (8000, 2, 2)
    # Avery bid $95: one step more is $96, shown rounded up to $100 so Avery's bid stays
    # private. Bidding $100 really would put Jordan first.
    assert (pi["to_be_first_cents"], pi["floor_cents"], pi["auctioned"]) == (10000, 5000, True)
    assert "tied" not in pi
    family = board(client, jordan)["family_law"]
    assert (family["my_bid_cents"], family["rank"], family["auctioned"]) == (None, None, False)
    assert (family["floor_cents"], family["to_be_first_cents"]) == (1500, None)

    avery = board(client, attorney_id(client, "Avery Stone"))["personal_injury"]
    assert (avery["rank"], avery["to_be_first_cents"]) == (1, None)  # nothing to reveal


def test_a_tie_for_first_is_not_shown_as_first(client):
    sam = attorney_id(client, "Sam Okafor")
    r = client.put(
        f"/api/v1/bids/{sam}",
        json={"bids_cents": {"personal_injury": 9500}},
        headers=bids_headers(sam),
    )
    pi = {row["practice_area"]: row for row in r.json()["areas"]}["personal_injury"]
    assert (pi["rank"], pi["to_be_first_cents"]) == (2, 10000)


def test_bid_to_be_first_is_never_more_than_the_page_accepts(client, settings):
    sam = attorney_id(client, "Sam Okafor")
    settings.max_bid_cents = 9500  # Avery already bids the maximum
    pi = board(client, sam)["personal_injury"]
    assert (pi["rank"], pi["to_be_first_cents"]) == (3, None)


def test_raising_a_bid_moves_the_attorney_up_and_the_next_lead_uses_it(client, db):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.put(
        f"/api/v1/bids/{jordan}",
        json={"bids_cents": {"personal_injury": 9900}},
        headers=bids_headers(jordan),
    )
    assert r.status_code == 200, r.text
    pi = {row["practice_area"]: row for row in r.json()["areas"]}["personal_injury"]
    assert (pi["my_bid_cents"], pi["rank"], pi["to_be_first_cents"]) == (9900, 1, None)

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
        ({"personal_injury": 1000}, "Personal Injury: the lowest bid accepted is $50.00"),
        ({"personal_injury": 900_000}, "the highest bid accepted is $5,000.00"),
        # In set-price areas the bid is the price itself, so the floor applies there too.
        ({"family_law": 0}, "Family Law: the lowest bid accepted is $15.00"),
        ({"employment": 1}, "Employment: the lowest bid accepted is $15.00"),
        ({"personal_injury": None, "workers_comp": None}, "keep at least one practice area"),
    ],
)
def test_bad_bids_are_explained_and_nothing_is_saved(client, bids, message):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.put(
        f"/api/v1/bids/{jordan}", json={"bids_cents": bids}, headers=bids_headers(jordan)
    )
    assert r.status_code == 422 and message in r.json()["detail"]
    rows = board(client, jordan)
    assert rows["personal_injury"]["my_bid_cents"] == 8000
    assert rows["family_law"]["my_bid_cents"] is None


@pytest.mark.parametrize("amount", [True, 85.5, "8500"])
def test_bids_must_be_whole_cents(client, amount):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.put(
        f"/api/v1/bids/{jordan}",
        json={"bids_cents": {"personal_injury": amount}},
        headers=bids_headers(jordan),
    )
    assert r.status_code == 422
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


def test_a_bid_left_below_a_raised_floor_says_it_is_not_in_line(client, settings):
    jordan = attorney_id(client, "Jordan Reyes")  # bids $60 in workers' comp
    settings.floor_price_cents = {**settings.floor_price_cents, "workers_comp": 7000}
    comp = board(client, jordan)["workers_comp"]
    assert (comp["my_bid_cents"], comp["below_floor"], comp["rank"]) == (6000, True, None)
    assert comp["to_be_first_cents"] == 7000


def test_subscribers_can_see_but_not_change_their_bids(client):
    morgan = attorney_id(client, "Morgan Lee")  # subscription plan
    page = bid_page(client, morgan)
    assert page["can_edit"] is False and "included in your subscription" in page["pricing_rule"]
    assert all(row["rank"] is None and row["to_be_first_cents"] is None for row in page["areas"])
    r = client.put(
        f"/api/v1/bids/{morgan}",
        json={"bids_cents": {"personal_injury": 500_000}},
        headers=bids_headers(morgan),
    )
    assert r.status_code == 403
    assert board(client, morgan)["personal_injury"]["my_bid_cents"] is None


def test_a_subscriber_in_an_auction_is_asked_after_the_paying_bidders(client):
    morgan = attorney_id(client, "Morgan Lee")
    r = client.patch(
        f"/api/v1/admin/attorneys/{morgan}",
        json={"bids_cents": {"family_law": 4000, "personal_injury": 500_000}},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    created = client.post("/api/v1/intake", json=inquiry_data()).json()
    detail = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
    line_up = sorted(detail["offers"], key=lambda o: o["round"])
    assert [o["price_cents"] for o in line_up] == [8100, 7100, 5000, 0]
    assert line_up[-1]["attorney_id"] == morgan
    # Paying bidders' board ignores the subscriber's bid: Jordan is still 2nd of 3.
    jordan = board(client, attorney_id(client, "Jordan Reyes"))["personal_injury"]
    assert (jordan["rank"], jordan["competing_bidders"]) == (2, 2)


@pytest.mark.parametrize(
    "bad",
    [None, "nope", "ééé", "offer-token", "next-version"],
)
def test_bid_pages_need_their_own_current_signed_link(client, bad):
    jordan = attorney_id(client, "Jordan Reyes")
    if bad == "offer-token":
        bad = links.sign_offer(SECRET, str(jordan))  # an offer token is not a bid token
    elif bad == "next-version":
        bad = links.sign_bids(SECRET, jordan, 1)  # a link that hasn't been sent yet
    headers = {} if bad is None else {"X-Attorney-Token": bad.encode("utf-8")}
    assert client.get(f"/api/v1/bids/{jordan}", headers=headers).status_code == 404
    assert client.get("/api/v1/bids/9999", headers=bids_headers(9999)).status_code == 404


def test_a_deactivated_attorney_link_stops_working(client):
    casey = attorney_id(client, "Casey Patel")
    assert client.get(f"/api/v1/bids/{casey}", headers=bids_headers(casey)).status_code == 200
    client.patch(f"/api/v1/admin/attorneys/{casey}", json={"active": False}, headers=ADMIN)
    assert client.get(f"/api/v1/bids/{casey}", headers=bids_headers(casey)).status_code == 404
    r = client.put(
        f"/api/v1/bids/{casey}",
        json={"bids_cents": {"family_law": 9900}},
        headers=bids_headers(casey),
    )
    assert r.status_code == 404
    assert (
        client.post(f"/api/v1/admin/attorneys/{casey}/bid-link", headers=ADMIN).status_code == 409
    )
    # Reactivating doesn't bring an old (possibly leaked) link back; a new one is needed.
    client.patch(f"/api/v1/admin/attorneys/{casey}", json={"active": True}, headers=ADMIN)
    assert client.get(f"/api/v1/bids/{casey}", headers=bids_headers(casey)).status_code == 404
    fresh = client.post(f"/api/v1/admin/attorneys/{casey}/bid-link", headers=ADMIN).json()["link"]
    again = {"X-Attorney-Token": fresh.split("#t=")[1]}
    assert client.get(f"/api/v1/bids/{casey}", headers=again).status_code == 200


def test_bid_saves_are_limited_per_day_and_recorded(client, settings):
    settings.max_bid_saves_per_day = 2
    jordan = attorney_id(client, "Jordan Reyes")

    def save(cents):
        return client.put(
            f"/api/v1/bids/{jordan}",
            json={"bids_cents": {"personal_injury": cents}},
            headers=bids_headers(jordan),
        )

    assert save(8500).status_code == 200
    assert save(8500).status_code == 200  # nothing changed: doesn't use up a save
    assert save(8600).status_code == 200
    refused = save(9900)
    assert refused.status_code == 429 and "2 times a day" in refused.json()["detail"]
    assert board(client, jordan)["personal_injury"]["my_bid_cents"] == 8600

    changes = client.get(
        "/api/v1/admin/bid-changes", params={"attorney_id": jordan}, headers=ADMIN
    ).json()
    assert [c["changes"] for c in changes] == [
        {"personal_injury": [8500, 8600]},
        {"personal_injury": [8000, 8500]},
    ]


def test_average_price_appears_only_after_five_sales_and_is_rounded(client):
    sam = attorney_id(client, "Sam Okafor")
    averages = []
    for n in range(5):
        created = client.post(
            "/api/v1/intake",
            json=inquiry_data(email=f"buyer{n}@example.org", phone=f"(312) 555-01{n}0"),
        ).json()
        detail = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
        first = next(o for o in detail["offers"] if o["round"] == 1)  # Avery, $81
        accepted = client.post(
            f"/api/v1/offers/{first['id']}/accept", json={"token": token(first["id"])}
        )
        assert accepted.status_code == 200, accepted.text
        pi = board(client, sam)["personal_injury"]
        averages.append((pi["sold_last_30_days"], pi["average_price_cents"]))
    # One sale would show its exact price (and so Jordan's bid + $1); five, rounded, don't.
    assert averages == [(1, None), (2, None), (3, None), (4, None), (5, 8000)]
    market = {
        m["practice_area"]: m for m in client.get("/api/v1/admin/market", headers=ADMIN).json()
    }
    assert market["personal_injury"]["average_price_cents"] == 8100  # staff see the exact figure


# --- Admin --------------------------------------------------------------------
def test_admin_emails_a_new_bid_link_that_replaces_the_old_one(client, mailbox):
    jordan = attorney_id(client, "Jordan Reyes")
    r = client.post(f"/api/v1/admin/attorneys/{jordan}/bid-link", headers=ADMIN)
    link = r.json()["link"]  # shown because the tests run in demo mode
    assert r.status_code == 200 and f"/bids/{jordan}#t=" in link
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    email = next(m for m in mailbox if m["subject"] == "Manage your lead bids")
    assert link in email["body"] and "replaces any earlier one" in email["body"]

    new = {"X-Attorney-Token": link.split("#t=")[1]}
    assert client.get(f"/api/v1/bids/{jordan}", headers=new).status_code == 200
    old = bids_headers(jordan, version=0)
    assert client.get(f"/api/v1/bids/{jordan}", headers=old).status_code == 404


def test_staff_cannot_set_a_pay_per_lead_price_below_the_floor(client, settings):
    new = {
        "name": "Low Price",
        "firm": "Example Firm (Demo)",
        "email": "low.price@example.com",
        "bar_states": ["IL"],
        "service_zips": ["606"],
        "bids_cents": {"bankruptcy": 1000},
    }
    r = client.post("/api/v1/admin/attorneys", json=new, headers=ADMIN)
    assert r.status_code == 422 and "Bankruptcy: the lowest price accepted is $15.00" in r.text
    # A subscriber's number isn't a price, so it may be anything.
    subscriber = {**new, "plan": "subscription", "monthly_fee_cents": 50000}
    assert client.post("/api/v1/admin/attorneys", json=subscriber, headers=ADMIN).status_code == 201

    casey = attorney_id(client, "Casey Patel")
    r = client.patch(
        f"/api/v1/admin/attorneys/{casey}",
        json={"bids_cents": {"bankruptcy": 1000}},
        headers=ADMIN,
    )
    assert r.status_code == 422

    # A price left below a floor raised later is flagged for staff.
    settings.floor_price_cents = {**settings.floor_price_cents, "workers_comp": 7000}
    market = {
        m["practice_area"]: m for m in client.get("/api/v1/admin/market", headers=ADMIN).json()
    }
    assert [b["below_floor"] for b in market["workers_comp"]["bidders"]] == [True]


def test_admin_sees_the_whole_market(client):
    market = {
        m["practice_area"]: m for m in client.get("/api/v1/admin/market", headers=ADMIN).json()
    }
    pi = market["personal_injury"]
    assert [b["bid_cents"] for b in pi["bidders"]] == [9500, 8000, 7000]
    assert (pi["model"], pi["floor_cents"], pi["sold_last_30_days"]) == ("ping_tree", 5000, 0)
    family = market["family_law"]
    assert ("subscription", 4000) in {(b["plan"], b["bid_cents"]) for b in family["bidders"]}


def test_offer_emails_explain_the_price_and_carry_no_bid_link(client, mailbox):
    client.post("/api/v1/intake", json=inquiry_data())
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    offer = next(m for m in mailbox if m["to"] == "avery.stone@example.com")
    assert "Market price: $1.00 more" in offer["body"]
    # Offer emails get forwarded to intake staff; the bid link is a lasting credential.
    assert "/bids/" not in offer["body"]


def test_the_price_note_describes_the_rule_used_when_the_offer_was_priced(client, settings):
    created = client.post("/api/v1/intake", json=inquiry_data()).json()
    detail = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
    first = next(o for o in detail["offers"] if o["round"] == 1)
    settings.auction_pricing, settings.bid_increment_cents = "first_price", 500  # changed later
    view = client.get(
        f"/api/v1/offers/{first['id']}", headers={"X-Offer-Token": token(first["id"])}
    )
    assert view.status_code == 200, view.text
    assert view.json()["price_note"].startswith("Market price: $1.00 more")


def test_subscribers_are_not_told_to_change_bids(client, mailbox):
    morgan = attorney_id(client, "Morgan Lee")  # subscription plan
    client.post(f"/api/v1/admin/attorneys/{morgan}/bid-link", headers=ADMIN)
    client.post("/api/v1/intake", json=inquiry_data(practice_area="family_law"))
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    to_morgan = {m["subject"]: m["body"] for m in mailbox if m["to"] == "morgan.lee@example.com"}
    assert "Manage your lead bids" in to_morgan and len(to_morgan) == 2  # bid link + offer
    assert all("change your bids" not in body for body in to_morgan.values())
    assert "you don't\nbid" in to_morgan["Manage your lead bids"]


def test_shared_offers_say_the_price_is_the_attorneys_own(client):
    created = client.post("/api/v1/intake", json=inquiry_data(practice_area="family_law")).json()
    detail = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
    notes = {
        o["price_cents"]: client.get(
            f"/api/v1/offers/{o['id']}", headers={"X-Offer-Token": token(o["id"])}
        ).json()["price_note"]
        for o in detail["offers"]
    }
    assert notes[0] == "Included in your plan."  # Morgan, on a subscription
    assert {notes[c] for c in notes if c} == {"Your set price for this practice area."}


def test_the_bids_page_is_served(client):
    assert "Your lead bids" in client.get("/bids/1").text
