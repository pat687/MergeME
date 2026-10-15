import pytest

from app.domain import compliance
from app.domain.types import ChargeKind
from tests.conftest import ADMIN


def test_monthly_billing_posts_flat_fees_once(client):
    first = client.post(
        "/api/v1/admin/billing/run", params={"period": "2026-10"}, headers=ADMIN
    ).json()
    again = client.post(
        "/api/v1/admin/billing/run", params={"period": "2026-10"}, headers=ADMIN
    ).json()
    # Morgan's subscription and Avery's territory, then nothing new the second time.
    assert (first["created"], first["skipped_existing"]) == (2, 0)
    assert (again["created"], again["skipped_existing"]) == (0, 2)


def test_statement_totals_each_attorney(client):
    client.post("/api/v1/admin/billing/run", params={"period": "2026-10"}, headers=ADMIN)
    avery = client.get(
        "/api/v1/admin/attorneys/1/statement", params={"period": "2026-10"}, headers=ADMIN
    )
    assert avery.json()["total_cents"] == 75000
    assert avery.json()["entries"][0]["kind"] == "territory"


def test_bad_billing_period_is_refused(client):
    r = client.post("/api/v1/admin/billing/run", params={"period": "2026-13"}, headers=ADMIN)
    assert r.status_code == 422


def test_percentage_pricing_cannot_even_be_submitted(client):
    attorney = {
        "name": "Pat Example",
        "firm": "Example Law (Demo)",
        "email": "pat@example.com",
        "bar_states": ["IL"],
        "service_zips": ["606"],
        "bids_cents": {"personal_injury": 5000},
        "fee_percent": 33,
    }
    r = client.post("/api/v1/admin/attorneys", json=attorney, headers=ADMIN)
    assert r.status_code == 422
    assert "fee_percent" in r.text

    del attorney["fee_percent"]
    assert client.post("/api/v1/admin/attorneys", json=attorney, headers=ADMIN).status_code == 201


@pytest.mark.parametrize(
    ("kind", "amount"),
    [("percent_of_settlement", 100), (ChargeKind.PER_LEAD, 0.33), (ChargeKind.PER_LEAD, -1)],
)
def test_ledger_gate_refuses_anything_but_flat_non_negative_cents(kind, amount):
    with pytest.raises(compliance.ComplianceError):
        compliance.assert_flat_fee(kind, amount)


def test_there_is_no_charge_kind_tied_to_case_outcomes():
    outcome_words = ("settle", "retain", "contingen", "percent", "share", "recovery")
    assert not [k for k in ChargeKind if any(w in k.value for w in outcome_words)]


def test_consumer_wording_never_claims_to_recommend(client):
    meta = client.get("/api/v1/meta").json()
    disclosure = meta["disclosure"].lower()
    assert "not a law firm" in disclosure
    assert "pay" in disclosure and "do not recommend" in disclosure
    for word in ("best", "top-rated", "matched you", "evaluated your case"):
        assert word not in disclosure
