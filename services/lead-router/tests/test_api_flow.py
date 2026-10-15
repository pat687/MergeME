"""End-to-end stories through the real HTTP API, using the six demo attorneys.

Demo network (see app/seed.py), all licensed in Illinois:
  Avery  PI $95 / med-mal $120       owns PI in 60614 exclusively
  Jordan PI $80 / workers' comp $60
  Sam    PI $70 / employment $50
  Morgan family $40 / estate $25     subscription plan
  Riley  criminal $45 / family $35
  Casey  bankruptcy $30 / family $20
"""

from __future__ import annotations

from datetime import timedelta

from app.db import utcnow
from app.models import Lead, LedgerEntry, Offer
from app.services import offers as offer_service
from tests.conftest import ADMIN, inquiry_data, token


def submit(client, **overrides) -> dict:
    r = client.post("/api/v1/intake", json=inquiry_data(**overrides))
    assert r.status_code == 201, r.text
    return r.json()


def detail(client, lead_id: str) -> dict:
    r = client.get(f"/api/v1/admin/leads/{lead_id}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def names(client) -> dict[int, str]:
    return {a["id"]: a["name"] for a in client.get("/api/v1/admin/attorneys", headers=ADMIN).json()}


def offer_for(client, lead: dict, attorney_name: str) -> dict:
    lookup = names(client)
    return next(o for o in lead["offers"] if lookup[o["attorney_id"]] == attorney_name)


def act(client, offer: dict, action: str, reason: str | None = None):
    return client.post(
        f"/api/v1/offers/{offer['id']}/{action}",
        json={"token": token(offer["id"]), "reason": reason},
    )


# --------------------------------------------------------------------------- #
# Ping tree
# --------------------------------------------------------------------------- #
def test_ping_tree_walks_down_the_line_until_someone_says_yes(client, app, settings, db):
    created = submit(client)  # personal injury in 60601 -> ping tree
    assert created["status"] == "offered"

    lead = detail(client, created["lead_id"])
    assert lead["distribution_model"] == "ping_tree"
    lineup = [(names(client)[o["attorney_id"]], o["status"]) for o in lead["offers"]]
    assert lineup == [
        ("Avery Stone", "pending"),
        ("Jordan Reyes", "queued"),
        ("Sam Okafor", "queued"),
    ]

    # Avery passes -> Jordan is asked next.
    assert (
        act(client, offer_for(client, lead, "Avery Stone"), "decline", "conflict").status_code
        == 200
    )
    lead = detail(client, created["lead_id"])
    assert offer_for(client, lead, "Jordan Reyes")["status"] == "pending"

    # Jordan never answers -> the worker times the offer out and asks Sam.
    later = utcnow() + timedelta(seconds=settings.ping_offer_ttl_seconds + 1)
    assert offer_service.expire_due(db, settings, later) == 1
    lead = detail(client, created["lead_id"])
    assert offer_for(client, lead, "Jordan Reyes")["status"] == "expired"
    sam = offer_for(client, lead, "Sam Okafor")
    assert sam["status"] == "pending"

    # Sam says yes and pays Sam's own bid, a flat $70.
    r = act(client, sam, "accept")
    assert r.status_code == 200
    assert r.json()["contact"]["phone"] == "3125550142"

    lead = detail(client, created["lead_id"])
    assert lead["status"] == "assigned"
    assert names(client)[lead["assigned_attorney_id"]] == "Sam Okafor"
    charge = db.query(LedgerEntry).filter_by(lead_id=created["lead_id"]).one()
    assert (charge.kind, charge.amount_cents) == ("per_lead", 7000)


def test_ping_tree_goes_unsold_when_everyone_passes(client):
    created = submit(client)
    for name in ("Avery Stone", "Jordan Reyes", "Sam Okafor"):
        lead = detail(client, created["lead_id"])
        assert act(client, offer_for(client, lead, name), "decline").status_code == 200
    lead = detail(client, created["lead_id"])
    assert lead["status"] == "unsold"
    assert lead["audit"][-1]["event"] == "unsold"


def test_ping_tree_skips_an_attorney_who_went_inactive_while_waiting(client):
    created = submit(client)
    jordan_id = next(k for k, v in names(client).items() if v == "Jordan Reyes")
    client.patch(f"/api/v1/admin/attorneys/{jordan_id}", json={"active": False}, headers=ADMIN)

    lead = detail(client, created["lead_id"])
    act(client, offer_for(client, lead, "Avery Stone"), "decline")
    lead = detail(client, created["lead_id"])
    assert offer_for(client, lead, "Jordan Reyes")["status"] == "skipped"
    assert offer_for(client, lead, "Sam Okafor")["status"] == "pending"


# --------------------------------------------------------------------------- #
# Shared (speed to lead)
# --------------------------------------------------------------------------- #
def test_shared_first_claim_wins_and_everyone_else_loses(client, db):
    created = submit(client, practice_area="family_law", injured=None, medical_treatment=None)
    lead = detail(client, created["lead_id"])
    assert lead["distribution_model"] == "shared"
    assert {o["status"] for o in lead["offers"]} == {"pending"}
    assert len(lead["offers"]) == 3

    riley, morgan = offer_for(client, lead, "Riley Chen"), offer_for(client, lead, "Morgan Lee")
    assert act(client, riley, "accept").status_code == 200

    too_late = act(client, morgan, "accept")
    assert too_late.status_code == 409
    assert too_late.json()["detail"] == "another attorney claimed this lead first"

    lead = detail(client, created["lead_id"])
    statuses = {names(client)[o["attorney_id"]]: o["status"] for o in lead["offers"]}
    assert statuses == {"Riley Chen": "accepted", "Morgan Lee": "lost", "Casey Patel": "lost"}
    assert db.query(LedgerEntry).filter_by(lead_id=created["lead_id"]).count() == 1


def test_subscription_attorney_is_charged_zero_per_lead(client, db):
    created = submit(client, practice_area="family_law", injured=None, medical_treatment=None)
    lead = detail(client, created["lead_id"])
    assert act(client, offer_for(client, lead, "Morgan Lee"), "accept").status_code == 200
    charge = db.query(LedgerEntry).filter_by(lead_id=created["lead_id"]).one()
    assert (charge.kind, charge.amount_cents) == ("included", 0)


def test_shared_goes_unsold_when_every_offer_expires(client, settings, db):
    created = submit(client, practice_area="bankruptcy", injured=None, medical_treatment=None)
    later = utcnow() + timedelta(seconds=settings.shared_offer_ttl_seconds + 1)
    assert offer_service.expire_due(db, settings, later) == 1
    assert detail(client, created["lead_id"])["status"] == "unsold"


# --------------------------------------------------------------------------- #
# Exclusive
# --------------------------------------------------------------------------- #
def test_exclusive_territory_is_assigned_instantly(client, db):
    created = submit(client, zip_code="60614")
    assert created["status"] == "assigned"
    lead = detail(client, created["lead_id"])
    assert lead["distribution_model"] == "exclusive"
    assert names(client)[lead["assigned_attorney_id"]] == "Avery Stone"
    charge = db.query(LedgerEntry).filter_by(lead_id=created["lead_id"]).one()
    assert (charge.kind, charge.amount_cents) == ("included", 0)  # covered by the territory fee


# --------------------------------------------------------------------------- #
# Leads that never reach an attorney
# --------------------------------------------------------------------------- #
def test_no_participating_attorney_points_the_consumer_to_the_bar(client):
    created = submit(client, practice_area="immigration", injured=None, medical_treatment=None)
    assert created["status"] == "unserviceable"
    assert "bar association" in created["message"]
    assert detail(client, created["lead_id"])["distribution_model"] is None


def test_duplicate_submission_is_rejected(client):
    submit(client)
    again = submit(client, first_name="J.")
    assert again["status"] == "rejected"
    assert any("duplicate" in r for r in detail(client, again["lead_id"])["rejection_reasons"])


def test_rejected_lead_does_not_block_a_corrected_resubmission(client):
    assert submit(client, consent=False)["status"] == "rejected"
    assert submit(client)["status"] == "offered"


def test_invalid_input_is_refused_before_anything_is_saved(client, db):
    r = client.post("/api/v1/intake", json=inquiry_data(zip_code="ABCDE"))
    assert r.status_code == 422
    assert db.query(Lead).count() == 0


# --------------------------------------------------------------------------- #
# Offer links and privacy
# --------------------------------------------------------------------------- #
def test_contact_details_stay_hidden_until_the_attorney_accepts(client):
    created = submit(client)
    avery = offer_for(client, detail(client, created["lead_id"]), "Avery Stone")
    preview = client.get(
        f"/api/v1/offers/{avery['id']}", headers={"X-Offer-Token": token(avery["id"])}
    ).json()
    assert preview["contact"] is None
    assert "3125550142" not in str(preview) and "Rivera" not in str(preview)

    accepted = act(client, avery, "accept").json()
    assert accepted["contact"]["name"] == "Jamie Rivera"


def test_a_forged_or_wrong_link_looks_like_a_missing_offer(client):
    created = submit(client)
    avery = offer_for(client, detail(client, created["lead_id"]), "Avery Stone")
    r = client.get(f"/api/v1/offers/{avery['id']}", headers={"X-Offer-Token": "0" * 32})
    assert r.status_code == 404
    r = client.post(f"/api/v1/offers/{avery['id']}/accept", json={"token": "nope"})
    assert r.status_code == 404


def test_queued_attorney_cannot_jump_the_line(client):
    created = submit(client)
    sam = offer_for(client, detail(client, created["lead_id"]), "Sam Okafor")
    r = act(client, sam, "accept")
    assert r.status_code == 409
    assert "queued" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Explainability and notifications
# --------------------------------------------------------------------------- #
def test_audit_trail_explains_who_was_skipped_and_why(client):
    created = submit(client)
    events = detail(client, created["lead_id"])["audit"]
    assert [e["event"] for e in events] == ["received", "scrubbed", "scored", "routing_planned"]
    skipped = events[-1]["detail"]["skipped"]
    assert skipped["Morgan Lee"] == "does not handle this practice area"


def test_emails_go_out_through_the_outbox(client, mailbox):
    submit(client)
    assert mailbox == []  # nothing is sent inside the request
    result = client.post("/api/v1/admin/worker/run", headers=ADMIN).json()
    assert result["delivered"] == 2
    recipients = sorted(m["to"] for m in mailbox)
    assert recipients == ["avery.stone@example.com", "jamie.rivera@example.org"]
    offer_mail = next(m for m in mailbox if m["to"] == "avery.stone@example.com")
    assert "3125550142" not in offer_mail["body"]  # no contact details before accepting
    assert "/offer/" in offer_mail["body"]


# --------------------------------------------------------------------------- #
# Admin surface
# --------------------------------------------------------------------------- #
def test_admin_endpoints_require_the_key(client):
    assert client.get("/api/v1/admin/leads").status_code == 401
    assert client.get("/api/v1/admin/leads", headers={"X-Admin-Key": "wrong"}).status_code == 401


def test_stats_summarise_the_pipeline(client):
    created = submit(client, practice_area="family_law", injured=None, medical_treatment=None)
    lead = detail(client, created["lead_id"])
    act(client, offer_for(client, lead, "Riley Chen"), "accept")
    stats = client.get("/api/v1/admin/stats", headers=ADMIN).json()
    assert stats["leads_by_status"] == {"assigned": 1}
    assert stats["revenue_cents_this_month"] == 3500
    assert stats["sell_through_rate"] == 1.0


def test_territories_are_truly_exclusive(client):
    r = client.post(
        "/api/v1/admin/territories",
        json={
            "attorney_id": 2,
            "practice_area": "personal_injury",
            "zip_code": "60614",
            "monthly_fee_cents": 1,
        },
        headers=ADMIN,
    )
    assert r.status_code == 409


def test_offer_rows_match_the_api(client, db):
    created = submit(client)
    assert db.query(Offer).filter_by(lead_id=created["lead_id"]).count() == 3
