"""Legal guardrails: where leads may be sold, what people agree to, attorneys' licenses,
and people's data (access, deletion, legal holds, retention).

The rules come from a legal audit whose sources couldn't be checked against the official
texts (docs/legal/COUNSEL_BRIEF.md). These tests pin down what the code does with them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import utcnow
from app.domain import compliance
from app.models import Attorney, AuditEvent, Lead, OutboxMessage
from tests.conftest import ADMIN, LAUNCH, add_attorney, inquiry_data, token


def lead_count(db) -> int:
    return db.query(Lead).count()


def detail(client, lead_id: str) -> dict:
    return client.get(f"/api/v1/admin/leads/{lead_id}", headers=ADMIN).json()


def attorney_id(client, name: str) -> int:
    return next(
        a["id"]
        for a in client.get("/api/v1/admin/attorneys", headers=ADMIN).json()
        if a["name"] == name
    )


def accept_first(client, lead_id: str) -> dict:
    first = next(o for o in detail(client, lead_id)["offers"] if o["status"] == "pending")
    r = client.post(f"/api/v1/offers/{first['id']}/accept", json={"token": token(first["id"])})
    assert r.status_code == 200, r.text
    return first


# --- Where leads may be sold -------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "WI", "zip_code": "53202"},  # not launched
        {"state": "WA", "zip_code": "98101"},  # not launched (and health data rules)
    ],
)
def test_inquiries_from_states_that_arent_open_are_refused_and_not_stored(
    client, db, mailbox, overrides
):
    before = lead_count(db)
    r = client.post("/api/v1/intake", json=inquiry_data(**overrides))
    assert r.status_code == 201
    body = r.json()
    assert (body["lead_id"], body["status"]) == (None, "unserviceable")
    assert "didn't keep" in body["message"] and "lawyer referral service" in body["message"]
    assert lead_count(db) == before
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    assert mailbox == []  # nothing sent to anyone


def test_flagged_areas_need_counsel_clearance_even_in_an_open_state(client, settings, db):
    settings.counsel_cleared = []  # Illinois injury areas are flagged (740 ILCS 92)
    assert settings.closed_reason("IL", "personal_injury").startswith("Insurance Claims Fraud")
    assert client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"] is None
    family = inquiry_data(practice_area="family_law", injured=None, medical_treatment=None)
    assert client.post("/api/v1/intake", json=family).json()["status"] == "offered"
    open_il = client.get("/api/v1/meta").json()["open"]["IL"]
    assert "family_law" in open_il and "personal_injury" not in open_il


def test_health_data_states_keep_injury_areas_closed_until_cleared():
    settings = Settings(_env_file=None, **{**LAUNCH, "launch_states": ["IL", "WA"]})
    assert "My Health My Data" in settings.closed_reason("WA", "personal_injury")
    assert settings.closed_reason("WA", "family_law") is None
    cleared = Settings(
        _env_file=None,
        **{**LAUNCH, "launch_states": ["WA"], "counsel_cleared": ["WA:personal_injury"]},
    )
    assert cleared.closed_reason("WA", "personal_injury") is None
    assert cleared.closed_reason("WA", "workers_comp") is not None


def test_operator_blocks_switch_off_an_area_or_a_whole_state(client, settings):
    settings.blocked_areas_by_state = {"IL": ["immigration"]}
    assert settings.closed_reason("IL", "immigration") == "turned off for this state"
    assert settings.closed_reason("IL", "family_law") is None
    settings.blocked_areas_by_state = {"IL": ["*"]}
    assert settings.closed_reason("IL", "family_law") == "turned off for this state"


@pytest.mark.parametrize("state", ["CA", "TX", "CO", "MI", "FL", "OH", "LA", "NJ", "GA"])
def test_launching_a_restricted_state_needs_recorded_counsel_approval(state):
    with pytest.raises(ValidationError, match="known restrictions"):
        Settings(_env_file=None, demo_mode=True, launch_states=[state])
    Settings(_env_file=None, demo_mode=True, launch_states=[state], counsel_cleared=[state])


def test_unknown_states_and_areas_in_the_launch_settings_are_refused():
    with pytest.raises(ValidationError, match="unknown state"):
        Settings(_env_file=None, demo_mode=True, launch_states=["XX"])
    with pytest.raises(ValidationError, match="unknown state"):
        Settings(_env_file=None, demo_mode=True, counsel_cleared=["IL:cooking"])


def test_production_refuses_unsafe_settings():
    with pytest.raises(ValidationError) as err:
        Settings(
            _env_file=None,
            demo_mode=False,
            public_base_url="http://leads.example.com",
            smtp_host="mailpit",
            require_license_verification=False,
        )
    text = str(err.value)
    for problem in ("https", "Mailpit", "LICENSE_VERIFICATION", "LEGAL_ENTITY_NAME"):
        assert problem in text
    ok = Settings(
        _env_file=None,
        demo_mode=False,
        public_base_url="https://leads.example.com",
        legal_entity_name="Example Leads LLC",
        postal_address="1 Main St, Chicago, IL 60601",
    )
    assert ok.operator == "Example Leads LLC, 1 Main St, Chicago, IL 60601"
    assert ok.operator in compliance.disclosure("X", ok.operator)


# --- What people are told and agree to ------------------------------------------
def test_disclosure_explains_selection_paid_priority_and_territories():
    text = compliance.disclosure("X")
    for phrase in (
        "Attorney advertising",
        "paid for by participating attorneys",
        "licensed in your state",
        "pay more per inquiry may be offered it first",
        "one attorney pays a monthly fee",
        "never a share of a legal fee",
        "may already represent someone else",
    ):
        assert phrase in text
    assert "flat fee" not in text  # auction prices vary from lead to lead


def test_consent_names_the_number_and_the_attestations(client, db):
    meta = client.get("/api/v1/meta").json()
    assert meta["phone_placeholder"] in meta["consent_text"]
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    lead = db.get(Lead, lead_id)
    assert "(312) 555-0142" in lead.consent_text
    for phrase in ("Please send my inquiry", "18 or older", "mine or ones I regularly use"):
        assert phrase in lead.consent_text
    assert "health information" in lead.sensitive_consent_text


def test_health_and_immigration_details_need_their_own_consent(client, db):
    before = lead_count(db)
    for area in ("personal_injury", "immigration"):
        r = client.post(
            "/api/v1/intake", json=inquiry_data(practice_area=area, sensitive_consent=False)
        )
        assert r.status_code == 422 and "we can't help" in r.text
    assert lead_count(db) == before  # refused before anything was stored
    family = inquiry_data(practice_area="family_law", sensitive_consent=False)
    lead_id = client.post("/api/v1/intake", json=family).json()["lead_id"]
    lead = db.get(Lead, lead_id)
    # Injury questions are dropped outside injury areas; no sensitive consent is recorded.
    assert (lead.injured, lead.medical_treatment, lead.sensitive_consent_text) == (None,) * 3


def test_federal_and_maritime_workers_comp_claims_are_not_taken(client, db):
    comp = {"practice_area": "workers_comp", "zip_code": "60607"}
    missing = client.post(
        "/api/v1/intake", json=inquiry_data(**comp, federal_or_maritime_worker=None)
    )
    assert missing.status_code == 422
    before = lead_count(db)
    federal = client.post(
        "/api/v1/intake", json=inquiry_data(**comp, federal_or_maritime_worker=True)
    ).json()
    assert federal["lead_id"] is None and "dol.gov/owcp" in federal["message"]
    assert lead_count(db) == before
    ok = client.post("/api/v1/intake", json=inquiry_data(**comp)).json()
    assert ok["lead_id"] is not None


def test_area_notices_cover_immigration_and_bankruptcy():
    notices = compliance.area_notices("X")
    assert "accredited representative" in notices["immigration"]
    assert "debt relief agencies" in notices["bankruptcy"]


def test_gpc_and_where_the_visit_came_from_are_recorded(client, db):
    lead_id = client.post(
        "/api/v1/intake",
        json=inquiry_data(utm_medium="cpc", utm_campaign="x" * 300, referrer_host="google.com"),
        headers={"Sec-GPC": "1"},
    ).json()["lead_id"]
    lead = db.get(Lead, lead_id)
    assert (lead.gpc, lead.utm_medium, lead.referrer_host) == (True, "cpc", "google.com")
    assert len(lead.utm_campaign) == 120


def test_the_person_is_told_which_attorney_has_their_details(client, mailbox):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    accept_first(client, lead_id)  # Avery Stone, first in line
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    to_jamie = {m["subject"]: m["body"] for m in mailbox if m["to"] == "jamie.rivera@example.org"}
    assigned = next(body for subject, body in to_jamie.items() if "attorney received" in subject)
    assert "Avery Stone" in assigned and "reply STOP" in assigned and "/privacy" in assigned
    receipt = next(body for subject, body in to_jamie.items() if "We received" in subject)
    assert "Please send my inquiry" in receipt and "info@legalleadrouter.com" in receipt


def test_privacy_page_is_served(client):
    assert "Privacy Policy" in client.get("/privacy").text


# --- Attorneys' licenses and agreement -----------------------------------------
def test_attorneys_without_a_recent_license_check_get_no_leads(client, db):
    avery = attorney_id(client, "Avery Stone")
    db.get(Attorney, avery).license_verified_at = utcnow() - timedelta(days=91)
    db.commit()
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    audit = next(e for e in detail(client, lead_id)["audit"] if e["event"] == "routing_planned")
    assert audit["detail"]["skipped"]["Avery Stone"] == "bar license not checked recently"

    r = client.post(
        f"/api/v1/admin/attorneys/{avery}/license-check",
        json={"license_numbers": {"WI": "1"}},
        headers=ADMIN,
    )
    assert r.status_code == 422 and "IL" in r.text  # every state they take leads in
    r = client.post(
        f"/api/v1/admin/attorneys/{avery}/license-check",
        json={"license_numbers": {"IL": "6301234"}},
        headers=ADMIN,
    )
    assert r.status_code == 200 and r.json()["license_numbers"] == {"IL": "6301234"}


def test_once_published_the_agreement_must_be_accepted(client, settings):
    settings.attorney_agreement_version = "2026-11"
    lead_id = client.post(
        "/api/v1/intake",
        json=inquiry_data(practice_area="family_law", injured=None, medical_treatment=None),
    ).json()["lead_id"]
    assert detail(client, lead_id)["status"] == "unserviceable"

    for name in ("Morgan Lee", "Riley Chen", "Casey Patel"):
        r = client.post(
            f"/api/v1/admin/attorneys/{attorney_id(client, name)}/agreement",
            json={"version": "2026-11"},
            headers=ADMIN,
        )
        assert r.status_code == 200 and r.json()["agreement_version"] == "2026-11"
    again = client.post(
        "/api/v1/intake",
        json=inquiry_data(
            practice_area="family_law",
            injured=None,
            medical_treatment=None,
            email="someone.else@example.org",
            phone="(312) 555-0199",
        ),
    ).json()
    assert again["status"] == "offered"


def test_a_license_lapse_is_caught_when_the_attorneys_turn_comes(client, db):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    jordan = attorney_id(client, "Jordan Reyes")  # second in line
    db.get(Attorney, jordan).license_verified_at = None
    db.commit()
    first = next(o for o in detail(client, lead_id)["offers"] if o["round"] == 1)
    client.post(f"/api/v1/offers/{first['id']}/decline", json={"token": token(first["id"])})
    rounds = {o["round"]: o for o in detail(client, lead_id)["offers"]}
    assert rounds[2]["status"] == "skipped"
    assert rounds[2]["decline_reason"] == "bar license not checked recently"
    assert rounds[3]["status"] == "pending"


def test_add_attorney_helper_records_a_check(client):
    made = add_attorney(
        client,
        {
            "name": "New Lawyer",
            "firm": "Example Firm (Demo)",
            "email": "new.lawyer@example.com",
            "bar_states": ["IL"],
            "service_zips": ["606"],
            "bids_cents": {"family_law": 2000},
        },
    )
    assert made["license_verified_at"] is not None


# --- People's data -------------------------------------------------------------
def test_a_person_can_get_everything_we_hold(client):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    accept_first(client, lead_id)
    found = client.get(
        "/api/v1/admin/privacy/lookup", params={"phone": "312-555-0142"}, headers=ADMIN
    ).json()
    assert [f["lead"]["id"] for f in found] == [lead_id]
    assert found[0]["lead"]["description"].startswith("I was rear-ended")
    assert found[0]["sent_to"][0]["attorney"].startswith("Avery Stone")
    assert client.get("/api/v1/admin/privacy/lookup", headers=ADMIN).status_code == 422


def test_erasing_removes_the_story_then_the_contact_details(client, db, mailbox):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    accept_first(client, lead_id)
    client.post("/api/v1/admin/worker/run", headers=ADMIN)  # emails go out
    mailbox.clear()

    r = client.post(
        f"/api/v1/admin/leads/{lead_id}/erase",
        json={"level": "narrative", "reason": "deletion request #1"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    lead = db.get(Lead, lead_id)
    assert (lead.description, lead.injured, lead.user_agent) == ("[erased]", None, None)
    assert lead.first_name == "Jamie"  # consent evidence is kept at this level
    sent = db.query(OutboxMessage).all()
    assert not any("rear-ended" in str(m.payload) for m in sent)
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    deletion = next(m for m in mailbox if m["subject"].startswith("Deletion request"))
    assert deletion["to"] == "avery.stone@example.com"

    client.post(
        f"/api/v1/admin/leads/{lead_id}/erase",
        json={"level": "full", "reason": "deletion request #1"},
        headers=ADMIN,
    )
    db.expire_all()
    lead = db.get(Lead, lead_id)
    assert (lead.first_name, lead.phone, lead.ip_address) == ("[erased]", "0000000000", None)
    assert "555-0142" not in lead.consent_text and "[erased]" in lead.consent_text
    events = [e.event for e in db.query(AuditEvent).filter_by(lead_id=lead_id)]
    assert events.count("erased") == 2


def test_erasing_a_lead_still_on_offer_withdraws_it(client, db):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    client.post(
        f"/api/v1/admin/leads/{lead_id}/erase",
        json={"level": "narrative", "reason": "changed their mind"},
        headers=ADMIN,
    )
    after = detail(client, lead_id)
    assert after["status"] == "unsold"
    assert {o["status"] for o in after["offers"]} == {"skipped"}


def test_a_legal_hold_stops_erasing_until_lifted(client, db, settings):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    hold = client.post(
        f"/api/v1/admin/leads/{lead_id}/legal-hold",
        json={"on": True, "reason": "TCPA demand letter 2026-10-15"},
        headers=ADMIN,
    )
    assert hold.json()["legal_hold"] is True
    refused = client.post(
        f"/api/v1/admin/leads/{lead_id}/erase",
        json={"level": "full", "reason": "deletion request"},
        headers=ADMIN,
    )
    assert refused.status_code == 409
    lead = db.get(Lead, lead_id)
    lead.status, lead.created_at = "rejected", utcnow() - timedelta(days=400)
    db.commit()
    client.post("/api/v1/admin/retention/run", headers=ADMIN)
    db.expire_all()
    assert db.get(Lead, lead_id).erased_at is None  # the schedule skips held leads too


def test_the_retention_schedule_erases_what_is_past_its_time(client, db):
    rejected = client.post("/api/v1/intake", json=inquiry_data(consent=False)).json()["lead_id"]
    sold = client.post(
        "/api/v1/intake", json=inquiry_data(email="b@example.org", phone="(312) 555-0177")
    ).json()["lead_id"]
    accept_first(client, sold)
    jordan = attorney_id(client, "Jordan Reyes")
    client.post(f"/api/v1/admin/attorneys/{jordan}/bid-link", headers=ADMIN)  # not about a lead
    client.post("/api/v1/admin/worker/run", headers=ADMIN)

    long_ago = utcnow() - timedelta(days=100)
    db.get(Lead, rejected).created_at = long_ago
    db.get(Lead, sold).assigned_at = long_ago
    for message in db.query(OutboxMessage).filter(OutboxMessage.sent_at.is_not(None)):
        message.sent_at = long_ago
    db.commit()

    counts = client.post("/api/v1/admin/retention/run", headers=ADMIN).json()
    assert counts["rejected"] == 1 and counts["narrative"] == 1 and counts["sent_email"] > 0
    db.expire_all()
    assert db.get(Lead, rejected).first_name == "[erased]"
    kept = db.get(Lead, sold)
    assert kept.description == "[erased]" and kept.first_name != "[erased]"
    assert all(
        m.payload.get("erased")
        for m in db.query(OutboxMessage).filter(OutboxMessage.sent_at.is_not(None))
    )
    # Nothing new is due a second time.
    again = client.post("/api/v1/admin/retention/run", headers=ADMIN).json()
    assert again["narrative"] == 0 and again["rejected"] == 0
