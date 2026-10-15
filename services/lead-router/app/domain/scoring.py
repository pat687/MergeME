"""Step 2b: give each lead a score from 0 to 100, and show the math.

Think of a teacher grading with a rubric: every rule adds or subtracts points
and writes down why. The total decides the tier (A, B or C). Because each
point has a written reason, anyone can ask "why is this lead a B?" and get a
real answer instead of "the algorithm said so".

The score is an internal quality signal shown to attorneys and staff (tier A/B/C
on every offer). It is NOT a judgement about anyone's legal case, it never
rejects a lead, and it's never shown to the consumer (see docs/COMPLIANCE.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date

from app.domain.types import PracticeArea
from app.schemas import IntakeRequest

# Starting points by practice area: roughly how much attorneys value an inquiry.
BASE_POINTS: dict[PracticeArea, int] = {
    PracticeArea.PERSONAL_INJURY: 30,
    PracticeArea.MEDICAL_MALPRACTICE: 30,
    PracticeArea.WORKERS_COMP: 25,
    PracticeArea.EMPLOYMENT: 20,
    PracticeArea.CRIMINAL_DEFENSE: 20,
    PracticeArea.FAMILY_LAW: 15,
    PracticeArea.BANKRUPTCY: 15,
    PracticeArea.IMMIGRATION: 15,
    PracticeArea.ESTATE_PLANNING: 10,
}
INJURY_AREAS = frozenset(
    {PracticeArea.PERSONAL_INJURY, PracticeArea.MEDICAL_MALPRACTICE, PracticeArea.WORKERS_COMP}
)
TIER_A, TIER_B = 70, 45


@dataclass(frozen=True)
class Factor:
    rule: str
    points: int
    reason: str


@dataclass(frozen=True)
class Score:
    total: int
    tier: str
    factors: list[Factor]
    flags: list[str]

    def breakdown(self) -> list[dict]:
        return [asdict(f) for f in self.factors]


Rule = Callable[[IntakeRequest, date], Factor | None]


def _base(i: IntakeRequest, today: date) -> Factor:
    pts = BASE_POINTS[i.practice_area]
    return Factor("practice_area", pts, f"{i.practice_area.label} inquiries start at {pts}")


def _recency(i: IntakeRequest, today: date) -> Factor | None:
    if i.incident_date is None:
        return None
    days = (today - i.incident_date).days
    if days < 0:
        return Factor("recency", -10, "incident date is in the future (likely a typo)")
    if days <= 30:
        return Factor("recency", 20, "incident happened in the last 30 days")
    if days <= 365:
        return Factor("recency", 10, "incident happened in the last year")
    return Factor("recency", 0, "incident is more than a year old")


def _injury(i: IntakeRequest, today: date) -> Factor | None:
    if i.practice_area not in INJURY_AREAS or not i.injured:
        return None
    if i.medical_treatment:
        return Factor("injury", 20, "reports an injury and received medical treatment")
    return Factor("injury", 10, "reports an injury")


def _urgency(i: IntakeRequest, today: date) -> Factor | None:
    if i.court_date_soon:
        return Factor("urgency", 15, "has a court date coming up soon")
    return None


def _represented(i: IntakeRequest, today: date) -> Factor | None:
    if i.has_attorney:
        return Factor("represented", -40, "already has an attorney for this matter")
    return None


def _detail(i: IntakeRequest, today: date) -> Factor | None:
    words = len(i.description.split())
    if words >= 40:
        return Factor("detail", 10, "gave a detailed description")
    if words >= 15:
        return Factor("detail", 5, "gave a reasonable description")
    return None


def _contactable(i: IntakeRequest, today: date) -> Factor:
    return Factor("contact", 10, "has a valid phone number and email")


RULES: list[Rule] = [_base, _recency, _injury, _urgency, _represented, _detail, _contactable]


def _flags(i: IntakeRequest, today: date) -> list[str]:
    """Notes for the attorney. They never block a lead; the attorney decides."""
    flags: list[str] = []
    if i.incident_date and (today - i.incident_date).days > 540:
        flags.append("time-sensitive: incident was over 18 months ago, review deadlines promptly")
    if i.has_attorney:
        flags.append("consumer says they already have an attorney")
    if i.court_date_soon:
        flags.append("consumer reports an upcoming court date")
    return flags


def score(inquiry: IntakeRequest, today: date) -> Score:
    factors = [f for rule in RULES if (f := rule(inquiry, today)) is not None]
    raw = sum(f.points for f in factors)
    total = max(0, min(100, raw))
    if total != raw:
        # Keep the math visible: the breakdown always adds up to the total.
        factors.append(Factor("limit", total - raw, "scores are kept between 0 and 100"))
    tier = "A" if total >= TIER_A else "B" if total >= TIER_B else "C"
    return Score(total=total, tier=tier, factors=factors, flags=_flags(inquiry, today))
