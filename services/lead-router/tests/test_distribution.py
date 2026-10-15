"""The routing rules, tested without a database."""

from app.domain.distribution import plan_distribution
from app.domain.matching import Candidate, covers_zip, ineligibility_reason
from app.domain.types import DistributionModel, OfferStatus

WHERE = {"practice_area": "personal_injury", "zip_code": "60601", "state": "IL"}


def lawyer(attorney_id: int, bid: int = 5000, **overrides) -> Candidate:
    data = {
        "attorney_id": attorney_id,
        "name": f"Attorney {attorney_id}",
        "active": True,
        "bar_states": ["IL"],
        "service_zips": ["606"],
        "bids_cents": {"personal_injury": bid},
        "plan": "pay_per_lead",
        "daily_cap": 10,
        "accepted_today": 0,
    }
    data.update(overrides)
    return Candidate(**data)


def plan(model, candidates, owner=None, shared_max=3, floor=1000):
    return plan_distribution(
        **WHERE,
        territory_owner=owner,
        candidates=candidates,
        policy_model=model,
        shared_max=shared_max,
        floor_cents=floor,
    )


# --- Matching --------------------------------------------------------------
def test_zip_prefix_and_exact_matching():
    assert covers_zip(["606"], "60614")
    assert covers_zip(["60614"], "60614")
    assert not covers_zip(["60614"], "60615")
    assert not covers_zip(["607"], "60614")


def test_each_eligibility_rule_has_a_reason():
    assert ineligibility_reason(lawyer(1, active=False), **WHERE) == "inactive"
    assert ineligibility_reason(lawyer(1, bar_states=["IN"]), **WHERE) == "not licensed in IL"
    assert "practice area" in ineligibility_reason(lawyer(1, bids_cents={"bankruptcy": 1}), **WHERE)
    assert "ZIP" in ineligibility_reason(lawyer(1, service_zips=["900"]), **WHERE)
    assert "cap" in ineligibility_reason(lawyer(1, daily_cap=2, accepted_today=2), **WHERE)
    assert ineligibility_reason(lawyer(1), **WHERE) is None


# --- Exclusive -------------------------------------------------------------
def test_exclusive_owner_gets_the_lead_automatically_at_no_extra_charge():
    owner = lawyer(9)
    result = plan(DistributionModel.SHARED, [lawyer(1), owner], owner=owner)
    assert result.model == DistributionModel.EXCLUSIVE
    assert [(o.attorney_id, o.status, o.price_cents) for o in result.offers] == [
        (9, OfferStatus.ACCEPTED, 0)
    ]


def test_unavailable_exclusive_owner_falls_back_to_the_normal_model():
    owner = lawyer(9, active=False)
    result = plan(DistributionModel.SHARED, [lawyer(1), owner], owner=owner)
    assert result.model == DistributionModel.SHARED
    assert result.skipped[9].startswith("exclusive owner unavailable")


# --- Shared ----------------------------------------------------------------
def test_shared_asks_up_to_n_attorneys_at_once():
    result = plan(DistributionModel.SHARED, [lawyer(i) for i in range(1, 6)], shared_max=3)
    assert len(result.offers) == 3
    assert {o.status for o in result.offers} == {OfferStatus.PENDING}
    assert len([r for r in result.skipped.values() if "pool already full" in r]) == 2


def test_shared_favours_attorneys_with_fewer_leads_today():
    busy, idle = lawyer(1, bid=9000, accepted_today=5), lawyer(2, bid=3000)
    result = plan(DistributionModel.SHARED, [busy, idle], shared_max=1)
    assert result.offers[0].attorney_id == 2


def test_subscribers_pay_nothing_per_lead():
    result = plan(DistributionModel.SHARED, [lawyer(1, plan="subscription")])
    assert result.offers[0].price_cents == 0


# --- Ping tree -------------------------------------------------------------
def test_ping_tree_lines_up_by_highest_bid():
    result = plan(DistributionModel.PING_TREE, [lawyer(1, 6000), lawyer(2, 9000), lawyer(3, 7000)])
    assert [o.attorney_id for o in result.offers] == [2, 3, 1]
    assert [o.status for o in result.offers] == [
        OfferStatus.PENDING,
        OfferStatus.QUEUED,
        OfferStatus.QUEUED,
    ]
    assert [o.round for o in result.offers] == [1, 2, 3]


def test_ping_tree_breaks_ties_by_fairness_then_id():
    result = plan(
        DistributionModel.PING_TREE,
        [lawyer(1, 8000, accepted_today=3), lawyer(2, 8000), lawyer(3, 8000)],
    )
    assert [o.attorney_id for o in result.offers] == [2, 3, 1]


def test_ping_tree_drops_bids_below_the_floor():
    result = plan(DistributionModel.PING_TREE, [lawyer(1, 500), lawyer(2, 9000)], floor=1000)
    assert [o.attorney_id for o in result.offers] == [2]
    assert "floor" in result.skipped[1]


def test_no_eligible_attorney_means_no_buyer():
    result = plan(DistributionModel.SHARED, [lawyer(1, bar_states=["WI"])])
    assert not result.has_buyer
    assert result.skipped == {1: "not licensed in IL"}
