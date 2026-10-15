"""Step 3a: which attorneys are allowed to receive this lead?

An attorney qualifies only if they are active, their bar license was checked
recently (and they accepted the current participation agreement, once there is
one), they are licensed in the consumer's state, handle this practice area,
cover this ZIP code and haven't hit their daily limit. If someone is skipped we
record why, so "why didn't I get that lead?" always has an answer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """A plain snapshot of an attorney, so routing logic never touches the database."""

    attorney_id: int
    name: str
    active: bool
    bar_states: list[str]
    service_zips: list[str]
    bids_cents: dict[str, int]
    plan: str
    daily_cap: int
    accepted_today: int
    # Set by the caller from the license check and agreement (offers.credentials_problem).
    credentials_problem: str | None = None

    def bid_for(self, practice_area: str) -> int:
        return self.bids_cents.get(practice_area, 0)


def covers_zip(service_zips: list[str], zip_code: str) -> bool:
    """'60614' matches '60614' exactly, or the 3-digit prefix '606' (a ZIP3 area)."""
    return any(zip_code == z or (len(z) == 3 and zip_code.startswith(z)) for z in service_zips)


def ineligibility_reason(
    c: Candidate, *, practice_area: str, zip_code: str, state: str
) -> str | None:
    if not c.active:
        return "inactive"
    if c.credentials_problem:
        return c.credentials_problem
    if state not in c.bar_states:
        return f"not licensed in {state}"
    if practice_area not in c.bids_cents:
        return "does not handle this practice area"
    if not covers_zip(c.service_zips, zip_code):
        return "does not cover this ZIP code"
    if c.accepted_today >= c.daily_cap:
        return "daily lead cap reached"
    return None
