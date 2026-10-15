"""Regression tests for the issues found in the adversarial review of PR #1."""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from app.api.pages import STATIC
from app.config import Settings
from app.db import utcnow
from app.domain import compliance
from app.domain.scoring import score
from app.main import create_app
from app.models import Lead, LedgerEntry, Offer, OutboxMessage
from app.seed import seed
from app.services import emails, offers, outbox
from tests.conftest import ADMIN, LAUNCH, SECRET, add_attorney, inquiry, inquiry_data, token

FAMILY = {"practice_area": "family_law", "injured": None, "medical_treatment": None}


def submit(client, n: int = 0, **overrides) -> dict:
    """Each n gets its own email and phone so submissions aren't duplicates."""
    data = inquiry_data(email=f"person{n}@example.org", phone=f"(312) 555-{1000 + n}", **overrides)
    r = client.post("/api/v1/intake", json=data)
    assert r.status_code == 201, r.text
    return r.json()


def lead_offers(client, lead_id: str) -> dict[str, dict]:
    names = {
        a["id"]: a["name"] for a in client.get("/api/v1/admin/attorneys", headers=ADMIN).json()
    }
    detail = client.get(f"/api/v1/admin/leads/{lead_id}", headers=ADMIN).json()
    return {names[o["attorney_id"]]: o for o in detail["offers"]}


def act(client, offer: dict, action: str):
    return client.post(f"/api/v1/offers/{offer['id']}/{action}", json={"token": token(offer["id"])})


def attorney_id(client, name: str) -> int:
    return next(
        a["id"]
        for a in client.get("/api/v1/admin/attorneys", headers=ADMIN).json()
        if a["name"] == name
    )


# --------------------------------------------------------------------------- #
# Daily cap and active flag are enforced when an attorney accepts
# --------------------------------------------------------------------------- #
def test_daily_cap_is_enforced_at_accept_time(client, db):
    riley = attorney_id(client, "Riley Chen")
    client.patch(f"/api/v1/admin/attorneys/{riley}", json={"daily_cap": 1}, headers=ADMIN)
    first, second = submit(client, 1, **FAMILY), submit(client, 2, **FAMILY)

    assert (
        act(client, lead_offers(client, first["lead_id"])["Riley Chen"], "accept").status_code
        == 200
    )
    over = act(client, lead_offers(client, second["lead_id"])["Riley Chen"], "accept")
    assert over.status_code == 409
    assert "daily lead cap reached" in over.json()["detail"]
    assert lead_offers(client, second["lead_id"])["Riley Chen"]["status"] == "skipped"
    assert db.query(LedgerEntry).filter_by(attorney_id=riley).count() == 1


def test_attorney_deactivated_while_offer_pending_cannot_accept(client):
    created = submit(client, 1, **FAMILY)
    riley = attorney_id(client, "Riley Chen")
    client.patch(f"/api/v1/admin/attorneys/{riley}", json={"active": False}, headers=ADMIN)
    r = act(client, lead_offers(client, created["lead_id"])["Riley Chen"], "accept")
    assert r.status_code == 409 and "inactive" in r.json()["detail"]


def test_ping_tree_moves_on_when_the_pending_attorney_is_over_cap(client):
    avery = attorney_id(client, "Avery Stone")
    client.patch(f"/api/v1/admin/attorneys/{avery}", json={"daily_cap": 1}, headers=ADMIN)
    first, second = submit(client, 1), submit(client, 2)  # both start with Avery pending
    assert (
        act(client, lead_offers(client, first["lead_id"])["Avery Stone"], "accept").status_code
        == 200
    )
    assert (
        act(client, lead_offers(client, second["lead_id"])["Avery Stone"], "accept").status_code
        == 409
    )
    line = lead_offers(client, second["lead_id"])
    assert (line["Avery Stone"]["status"], line["Jordan Reyes"]["status"]) == ("skipped", "pending")


def test_daily_cap_resets_at_local_midnight_not_utc():
    # 02:00 UTC on Oct 15 is still Oct 14 in Chicago (UTC-5 in October).
    start = offers.start_of_day(datetime(2026, 10, 15, 2, 0, tzinfo=UTC), "America/Chicago")
    assert start == datetime(2026, 10, 14, 5, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Stale writers can't overwrite newer state (matters on SQLite, no row locks)
# --------------------------------------------------------------------------- #
def test_stale_advance_cannot_mark_an_assigned_lead_unsold(tmp_path, mailbox):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'race.db'}",
        admin_api_key="test-admin",
        link_signing_secret=SECRET,
        embedded_worker=False,
        **LAUNCH,
    )
    app = create_app(settings, handlers={"email": mailbox.append})
    factory = app.state.session_factory
    with factory() as db:
        seed(db)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        lead_id = submit(client, 1, **FAMILY)["lead_id"]
        stale = factory()
        stale_lead = stale.get(Lead, lead_id)  # read while still "offered"
        stale.commit()
        riley = lead_offers(client, lead_id)["Riley Chen"]
        assert act(client, riley, "accept").status_code == 200

        stale_lead.status = "offered"  # what a racing reader still believes
        offers.advance(stale, settings, stale_lead, utcnow())
        stale.commit()
        stale.close()
        detail = client.get(f"/api/v1/admin/leads/{lead_id}", headers=ADMIN).json()
        assert detail["status"] == "assigned"


def test_decline_after_someone_else_won_is_refused(client):
    created = submit(client, 1, **FAMILY)
    line = lead_offers(client, created["lead_id"])
    act(client, line["Riley Chen"], "accept")
    r = act(client, line["Morgan Lee"], "decline")
    assert r.status_code == 409 and "claimed" in r.json()["detail"]
    detail = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
    assert detail["status"] == "assigned"


# --------------------------------------------------------------------------- #
# Consent: never share with more attorneys than the person agreed to
# --------------------------------------------------------------------------- #
def test_every_model_respects_the_consented_number_of_attorneys(client, settings):
    for n in range(4):  # make 7 personal-injury bidders in total
        add_attorney(
            client,
            {
                "name": f"Extra Bidder {n}",
                "firm": "Example Firm (Demo)",
                "email": f"extra{n}@example.com",
                "bar_states": ["IL"],
                "service_zips": ["606"],
                "bids_cents": {"personal_injury": 6000 + n},
            },
        )
    assert (
        f"up to {settings.shared_max_attorneys}"
        in client.get("/api/v1/meta").json()["consent_text"]
    )
    # Shared: never more simultaneous offers than the consent allows.
    shared = submit(client, 1, **FAMILY)
    assert len(lead_offers(client, shared["lead_id"])) <= settings.shared_max_attorneys

    # Ping tree: everyone lines up, but only N are ever asked; then the lead goes unsold.
    lead_id = submit(client, 2)["lead_id"]
    asked = []
    while pending := [o for o in lead_offers(client, lead_id).values() if o["status"] == "pending"]:
        asked.append(pending[0]["attorney_id"])
        assert act(client, pending[0], "decline").status_code == 200
    line = lead_offers(client, lead_id).values()
    assert len(asked) == settings.shared_max_attorneys
    assert sum("line-up full" in (o["decline_reason"] or "") for o in line) == 7 - len(asked)
    detail = client.get(f"/api/v1/admin/leads/{lead_id}", headers=ADMIN).json()
    assert detail["status"] == "unsold"


def test_ping_tree_attorney_skipped_while_queued_does_not_use_up_a_place(client, settings):
    for n in range(2):
        add_attorney(
            client,
            {
                "name": f"Late Bidder {n}",
                "firm": "Example Firm (Demo)",
                "email": f"late{n}@example.com",
                "bar_states": ["IL"],
                "service_zips": ["606"],
                "bids_cents": {"personal_injury": 6000 + n},
            },
        )
    lead_id = submit(client, 1)["lead_id"]  # line-up: Avery, Jordan, Sam, Late 1, Late 0
    jordan = attorney_id(client, "Jordan Reyes")
    client.patch(f"/api/v1/admin/attorneys/{jordan}", json={"active": False}, headers=ADMIN)
    for name in ("Avery Stone", "Sam Okafor", "Late Bidder 1"):
        offer = lead_offers(client, lead_id)[name]
        assert offer["status"] == "pending", name
        assert act(client, offer, "decline").status_code == 200
    line = lead_offers(client, lead_id)
    assert (line["Jordan Reyes"]["status"], line["Jordan Reyes"]["decline_reason"]) == (
        "skipped",
        "inactive",
    )
    assert line["Late Bidder 0"]["status"] == "skipped"
    assert "line-up full" in line["Late Bidder 0"]["decline_reason"]
    late = line["Late Bidder 0"]
    view = client.get(f"/api/v1/offers/{late['id']}", headers={"X-Offer-Token": token(late["id"])})
    assert "agreed number of attorneys" in view.json()["notice"]


def test_consent_given_on_an_old_form_version_is_rejected(client):
    created = submit(client, 1, consent_version="2025-01-01")
    assert created["status"] == "rejected"
    assert created["reload_form"] is True and "submit again" in created["message"]
    reasons = client.get(f"/api/v1/admin/leads/{created['lead_id']}", headers=ADMIN).json()
    assert any("outdated form" in r for r in reasons["rejection_reasons"])


def test_overlong_consent_version_is_a_422_not_a_500(client):
    r = client.post("/api/v1/intake", json=inquiry_data(consent_version="x" * 41))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Privacy: free-text contact details don't leak before acceptance
# --------------------------------------------------------------------------- #
LEAKY = (
    "My name is Jamie Rivera, call me at 312-555-0142 or jamie.rivera@example.org. "
    "I live at 12 Elm St. I was rear-ended and my back hurts a lot."
)


def test_offer_preview_and_email_redact_contact_details(client, mailbox):
    created = client.post("/api/v1/intake", json=inquiry_data(description=LEAKY)).json()
    avery = lead_offers(client, created["lead_id"])["Avery Stone"]
    preview = client.get(
        f"/api/v1/offers/{avery['id']}", headers={"X-Offer-Token": token(avery["id"])}
    ).json()
    client.post("/api/v1/admin/worker/run", headers=ADMIN)
    offer_mail = next(m for m in mailbox if m["to"] == "avery.stone@example.com")

    for shown in (preview["summary"], offer_mail["body"]):
        for secret in ("Jamie", "Rivera", "312-555-0142", "jamie.rivera@example.org", "12 Elm St"):
            assert secret not in shown
        assert "[redacted]" in shown and "rear-ended" in shown

    accepted = act(client, avery, "accept").json()
    assert accepted["contact"]["description"] == LEAKY  # full text after acceptance


def test_redaction_catches_lowercase_names_addresses_and_dashed_phones():
    text = "dana quillfeather here, i live at 12 elm st, call 312\u2013555\u20130199"
    out = compliance.redact(text, names=["Dana", "Quillfeather"], phone="3125550199")
    for secret in ("dana", "quillfeather", "12 elm st", "0199"):
        assert secret not in out.lower()


def test_redaction_catches_hyphenated_names_slashed_phones_and_bare_addresses():
    text = (
        "garcia and Lopez here (Jamie Garcia-Lopez). Call 312/555/0142 or 3 1 2 5 5 5 0 1 4 2. "
        "I live at 4417 N Kedzie, my address is 88 Wabash Ave."
    )
    out = compliance.redact(text, names=["Jamie", "Garcia-Lopez"], phone="3125550142")
    for secret in ("garcia", "lopez", "jamie", "0142", "0 1 4 2", "4417", "kedzie", "wabash"):
        assert secret not in out.lower(), (secret, out)


def test_redaction_leaves_everyday_phrases_with_numbers_alone():
    text = "I was hit by 3 cars on the way home, 2 blocks down the street."
    assert compliance.redact(text, names=["Jo", "Smith"]) == text


def test_redaction_leaves_courts_and_doctor_visits_alone():
    text = "In 2019 Family Court gave me custody. Since then I've had 6 Dr appointments."
    assert compliance.redact(text, names=["Jo", "Smith"]) == text


def test_redaction_keeps_ordinary_words():
    text = "I will tell Will that 3 cars hit me down the road."
    assert compliance.redact(text, names=["Will", "Smith"]) == (
        "I will tell [redacted] that 3 cars hit me down the road."
    )


def test_offer_links_keep_the_token_out_of_server_logs(client):
    created = submit(client, 1)
    link = lead_offers(client, created["lead_id"])["Avery Stone"]["link"]
    assert "#t=" in link and "?t=" not in link


def test_pages_cached_before_the_header_switch_still_load_offers(client):
    offer = lead_offers(client, submit(client, 1)["lead_id"])["Avery Stone"]
    old_style = client.get(f"/api/v1/offers/{offer['id']}?t={token(offer['id'])}")
    assert old_style.status_code == 200
    assert client.get(f"/api/v1/offers/{offer['id']}").status_code == 404


@pytest.mark.parametrize("path", ["/", "/admin", "/offer/x", "/static/common.js"])
def test_pages_and_scripts_are_revalidated_after_a_release(client, path):
    assert client.get(path).headers["cache-control"] == "no-cache"


def test_pending_offer_view_reports_server_side_time_left(client):
    created = submit(client, 1)
    avery = lead_offers(client, created["lead_id"])["Avery Stone"]
    view = client.get(
        f"/api/v1/offers/{avery['id']}", headers={"X-Offer-Token": token(avery["id"])}
    ).json()
    assert 0 < view["seconds_left"] <= 90


# --------------------------------------------------------------------------- #
# Input hardening
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "customer. Your refund: https://evil.example/claim",
        "bob@example.com",
        "R2D2",
        "Visit secure.barrgo-login.net",
    ],
)
def test_names_cannot_carry_links_emails_or_digits(client, name):
    assert client.post("/api/v1/intake", json=inquiry_data(first_name=name)).status_code == 422


@pytest.mark.parametrize(
    "name",
    [
        "O'Brien-Smith Jr.",
        "José",
        "Nguyễn",
        "J. R. Smith",
        "Smith, Jr.",
        "प्रिया",  # Devanagari: the vowel sign and virama are combining marks
        "முருகன்",  # Tamil
        "\u0645\u06cc\u200c\u0646\u0627",  # Persian, typed with a zero-width non-joiner
    ],
)
def test_real_names_are_accepted(name):
    assert inquiry(first_name=name).first_name == name


def test_names_typed_with_separate_accents_are_normalized():
    assert inquiry(first_name="Jose\u0301").first_name == "Jos\u00e9"


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("60614-1234", True),
        ("606141234", True),
        ("60614 1234", True),
        ("60614-123", False),
        ("606141", False),
        ("60614abc", False),
        ("\u0666\u0660\u0666\u0660\u0661", False),  # Arabic-Indic digits
        ("\uff16\uff10\uff16\uff10\uff11", False),  # full-width digits
    ],
)
def test_zip_codes_must_be_exactly_five_digits_or_zip_plus_four(raw, ok):
    if ok:
        assert inquiry(zip_code=raw).zip_code == "60614"
    else:
        with pytest.raises(ValueError):
            inquiry(zip_code=raw)


def test_non_ascii_tokens_and_keys_get_404_and_401_not_500(client):
    created = submit(client, 1)
    offer_id = lead_offers(client, created["lead_id"])["Avery Stone"]["id"]
    r = client.post(f"/api/v1/offers/{offer_id}/accept", json={"token": "é"})
    assert r.status_code == 404
    raw = "é".encode("latin-1")  # how a non-ASCII header arrives on the wire
    assert client.get("/api/v1/admin/leads", headers={"X-Admin-Key": raw}).status_code == 401


def test_intake_is_rate_limited_per_ip(mailbox):
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        embedded_worker=False,
        intake_rate_limit_per_minute=2,
        **LAUNCH,
    )
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, handlers={"email": mailbox.append})) as client:
        codes = [
            client.post("/api/v1/intake", json=inquiry_data(email=f"p{i}@example.org")).status_code
            for i in range(3)
        ]
    assert codes == [201, 201, 429]


def test_unusable_client_address_is_not_stored_as_consent_evidence(client, db):
    created = submit(client, 1)
    assert db.get(Lead, created["lead_id"]).ip_address is None  # TestClient's host isn't an IP


# --------------------------------------------------------------------------- #
# Admin updates can't corrupt the attorney table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "patch",
    [
        {"bids_cents": None},
        {"service_zips": None},
        {"active": None},
        {"bids_cents": {"family_law": -500}},
        {"service_zips": []},
        {"service_zips": ["60614-1234"]},
    ],
)
def test_bad_attorney_patches_are_rejected_before_saving(client, patch):
    riley = attorney_id(client, "Riley Chen")
    assert (
        client.patch(f"/api/v1/admin/attorneys/{riley}", json=patch, headers=ADMIN).status_code
        == 422
    )
    assert client.get("/api/v1/admin/attorneys", headers=ADMIN).status_code == 200
    assert submit(client, 1, **FAMILY)["status"] == "offered"


def test_territory_owner_must_handle_the_area_and_cover_the_zip(client):
    casey = attorney_id(client, "Casey Patel")  # bankruptcy/family, ZIPs 605 + 606
    wrong_area = {"attorney_id": casey, "practice_area": "personal_injury", "zip_code": "60601"}
    wrong_zip = {"attorney_id": casey, "practice_area": "bankruptcy", "zip_code": "90210"}
    for body in (wrong_area, wrong_zip):
        r = client.post(
            "/api/v1/admin/territories", json={**body, "monthly_fee_cents": 100}, headers=ADMIN
        )
        assert r.status_code == 422


def test_inactive_territory_owner_is_not_charged_rent(client):
    avery = attorney_id(client, "Avery Stone")
    client.patch(f"/api/v1/admin/attorneys/{avery}", json={"active": False}, headers=ADMIN)
    client.post("/api/v1/admin/billing/run", params={"period": "2026-10"}, headers=ADMIN)
    statement = client.get(
        f"/api/v1/admin/attorneys/{avery}/statement", params={"period": "2026-10"}, headers=ADMIN
    ).json()
    assert statement["total_cents"] == 0


# --------------------------------------------------------------------------- #
# Explainability and wording
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides",
    [
        {"court_date_soon": True, "description": " ".join(["detail"] * 45)},  # sums above 100
        {"practice_area": "estate_planning", "has_attorney": True, "incident_date": None},
    ],
)
def test_score_breakdown_always_adds_up_to_the_total(overrides):
    result = score(inquiry(**overrides), date(2026, 10, 15))
    assert sum(f.points for f in result.factors) == result.total


BANNED = ("best", "top-rated", "matched you", "evaluated your case", "recommend you")


def test_no_consumer_facing_text_claims_a_recommendation(settings):
    texts = [compliance.consent_text("X", 3), compliance.disclosure("X")]
    texts += [
        compliance.consumer_message(s, "X")
        for s in ("offered", "assigned", "unserviceable", "rejected", "other")
    ]
    texts.append(compliance.consumer_message("rejected", "X", [compliance.OUTDATED_FORM]))
    lead = Lead(
        id="abcdef12-0000-0000-0000-000000000000",
        first_name="Jamie",
        email="j@example.org",
        status="offered",
    )
    receipt = emails.consumer_receipt(settings, lead)
    texts += [receipt["subject"], receipt["body"]]
    # The intake page's own headings and copy (what a consumer reads before submitting).
    page = (STATIC / "intake.html").read_text()
    texts.append(re.sub(r"<script.*?</script>|<[^>]+>", " ", page, flags=re.DOTALL))
    for text in texts:
        for phrase in BANNED:
            assert phrase not in text.lower(), (phrase, text[:200])


def test_receipt_contains_no_text_the_submitter_typed(settings):
    lead = Lead(
        id="abcdef12-0000-0000-0000-000000000000",
        first_name="Visit secure barrgo-login",
        email="victim@example.org",
        status="unserviceable",
    )
    assert "barrgo-login" not in emails.consumer_receipt(settings, lead)["body"]


# --------------------------------------------------------------------------- #
# Worker: slow deliveries can't starve offer timeouts
# --------------------------------------------------------------------------- #
def test_outbox_delivery_stops_when_its_time_budget_is_spent(app, db):
    for i in range(5):
        outbox.enqueue(db, "email", {"to": f"x{i}@example.com", "subject": "s", "body": "b"})
    db.commit()

    def slow(_payload):
        time.sleep(0.2)

    delivered = outbox.deliver_due(
        app.state.session_factory,
        {"email": slow},
        now=utcnow(),
        max_attempts=5,
        time_budget_seconds=0.3,
    )
    db.expire_all()
    untouched = db.query(OutboxMessage).filter_by(sent_at=None, attempts=0).count()
    assert delivered == 2 and untouched == 3  # the rest wait for the next cycle


def test_shared_offers_still_expire_after_ttl(client, settings, db):
    created = submit(client, 1, **FAMILY)
    later = utcnow() + timedelta(seconds=settings.shared_offer_ttl_seconds + 1)
    assert offers.expire_due(db, settings, later) == 3
    assert db.get(Lead, created["lead_id"]).status == "unsold"
    assert db.query(Offer).filter_by(lead_id=created["lead_id"], status="expired").count() == 3


def test_long_marketing_tags_are_trimmed_not_rejected(client, db):
    r = client.post("/api/v1/intake", json=inquiry_data(utm_source="x" * 300))
    assert r.status_code == 201, r.text
    assert db.get(Lead, r.json()["lead_id"]).utm_source == "x" * 120


# --------------------------------------------------------------------------- #
# Fourth review round
# --------------------------------------------------------------------------- #
def _bidders(client, n: int) -> None:
    for i in range(n):
        add_attorney(
            client,
            {
                "name": f"Round4 Bidder {i}",
                "firm": "Example Firm (Demo)",
                "email": f"round4.{i}@example.com",
                "bar_states": ["IL"],
                "service_zips": ["606"],
                "bids_cents": {"personal_injury": 6000 + i},
            },
        )


def test_consent_limit_is_the_number_agreed_to_not_todays_setting(client, settings):
    _bidders(client, 3)  # six personal-injury bidders in all
    lead_id = submit(client, 1)["lead_id"]
    settings.shared_max_attorneys = 5  # raised while the lead is in flight
    while pending := [o for o in lead_offers(client, lead_id).values() if o["status"] == "pending"]:
        assert act(client, pending[0], "decline").status_code == 200
    asked = [o for o in lead_offers(client, lead_id).values() if o["activated_at"]]
    assert len(asked) == 3


def test_line_up_closes_as_soon_as_the_last_allowed_attorney_is_asked(client):
    _bidders(client, 3)
    lead_id = submit(client, 1)["lead_id"]
    for name in ("Avery Stone", "Jordan Reyes"):
        assert act(client, lead_offers(client, lead_id)[name], "decline").status_code == 200
    line = lead_offers(client, lead_id)
    assert line["Sam Okafor"]["status"] == "pending"  # the third and last one asked
    closed = [o for o in line.values() if "line-up full" in (o["decline_reason"] or "")]
    assert len(closed) == 3 and all(o["status"] == "skipped" for o in closed)
    trail = client.get(f"/api/v1/admin/leads/{lead_id}", headers=ADMIN).json()["audit"]
    assert [a["event"] for a in trail].count("line_up_full") == 1  # one entry, not one each


def test_shared_max_attorneys_must_be_at_least_one():
    with pytest.raises(ValueError):
        Settings(_env_file=None, shared_max_attorneys=0)


def test_older_databases_get_the_new_columns_at_startup(tmp_path):
    from sqlalchemy import inspect, text

    from app.db import ADDED_COLUMNS, Base, make_engine, upgrade_schema

    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, _type in ADDED_COLUMNS:  # as an old release made them
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(
            text(
                "INSERT INTO attorneys (name, firm, email, bar_states, service_zips, bids_cents,"
                " plan, monthly_fee_cents, daily_cap, active, created_at) VALUES ('A', 'F',"
                " 'a@example.com', '[]', '[]', '{}', 'pay_per_lead', 0, 1, 1, '2026-10-01')"
            )
        )
    upgrade_schema(engine)
    upgrade_schema(engine)  # and again: safe to repeat
    for table, column, _type in ADDED_COLUMNS:
        assert column in {c["name"] for c in inspect(engine).get_columns(table)}
    with engine.connect() as conn:  # an existing attorney's bid link starts at version 0
        assert conn.execute(text("SELECT bids_link_version FROM attorneys")).scalar_one() == 0


@pytest.mark.parametrize(
    ("name", "description"),
    [
        ("জয়", "Hi, I'm জয় and I was hurt at work."),  # Bengali, precomposed
        ("Nguyễn", "This is Nguyễn, rear-ended last week."),  # typed accents
        ("Prescott,Jr.", "Mr. Prescott here, rear-ended last week."),
    ],
)
def test_names_are_redacted_whatever_unicode_form_or_comma_they_use(name, description):
    lead = Lead(
        first_name="Pat",
        last_name=inquiry(last_name=name).last_name,
        description=description,
        email="x@example.org",
        phone="3125550100",
    )
    summary = lead.summary()
    assert "[redacted]" in summary
    for part in ("জ", "Nguy", "Prescott"):
        assert part not in summary, summary


@pytest.mark.parametrize(
    "address",
    ["18 County Line Ct", "1234 City View Ct", "77 Justice Court", "300 Family Tree Ct"],
)
def test_streets_named_after_courts_are_still_addresses(address):
    assert compliance.redact(f"I live near {address} with my kids.") == (
        "I live near [redacted] with my kids."
    )


def test_court_names_with_years_are_kept_including_immigration_and_possessives():
    text = (
        "My 2024 Immigration Court hearing was moved. In 2019 Surrogate's Court and "
        "2021 Common Pleas Court the cases were closed."
    )
    assert compliance.redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "I have lived at 4417 Kedzie for years.",
        "My address is: 88 Wabash",
        "i live at 918 n albany",
    ],
)
def test_more_ways_of_writing_an_address_are_redacted(text):
    out = compliance.redact(text)
    for secret in ("4417", "kedzie", "88", "wabash", "918", "albany"):
        assert secret not in out.lower(), out


def test_ordinary_sentences_after_lived_at_are_left_alone():
    text = "We lived at 2 different places last year."
    assert compliance.redact(text) == text


def test_names_that_grow_under_nfc_get_a_422_not_a_500(client):
    r = client.post("/api/v1/intake", json=inquiry_data(first_name="שּׁ" * 40))
    assert r.status_code == 422


def test_control_characters_are_removed_not_stored(client, db):
    data = inquiry_data(utm_source="spring\x00sale", description="My back hurts\x00 after a crash.")
    r = client.post("/api/v1/intake", json=data)
    assert r.status_code == 201, r.text
    lead = db.get(Lead, r.json()["lead_id"])
    assert (lead.utm_source, lead.description) == ("springsale", "My back hurts after a crash.")


def test_pages_answer_304_when_unchanged(client):
    first = client.get("/admin")
    again = client.get("/admin", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"
