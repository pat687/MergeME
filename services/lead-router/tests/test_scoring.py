from app.domain.scoring import score
from tests.conftest import TODAY, inquiry


def test_recent_treated_injury_is_tier_a():
    result = score(inquiry(), TODAY)
    assert result.tier == "A"
    assert result.total >= 70


def test_breakdown_explains_every_point():
    result = score(inquiry(), TODAY)
    assert sum(f.points for f in result.factors) == result.total
    assert all(f.reason for f in result.factors)


def test_already_represented_drops_the_score_and_is_flagged():
    base = score(inquiry(), TODAY)
    represented = score(inquiry(has_attorney=True), TODAY)
    assert represented.total == base.total - 40
    assert "consumer says they already have an attorney" in represented.flags


def test_score_never_goes_below_zero():
    low = inquiry(
        practice_area="estate_planning",
        has_attorney=True,
        incident_date=None,
        injured=None,
        description="Need help with a will for my mom please.",
    )
    assert score(low, TODAY).total == 0
    assert score(low, TODAY).tier == "C"


def test_old_incident_gets_deadline_flag_but_is_not_rejected():
    result = score(inquiry(incident_date="2024-01-10"), TODAY)
    assert any("time-sensitive" in f for f in result.flags)
    assert result.total > 0


def test_court_date_adds_urgency():
    calm = score(inquiry(practice_area="criminal_defense"), TODAY)
    urgent = score(inquiry(practice_area="criminal_defense", court_date_soon=True), TODAY)
    assert urgent.total - calm.total == 15
