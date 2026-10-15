"""The vocabulary of the system: the fixed lists of words every other module uses.

Keeping them in one place means a typo like "personal-injury" vs
"personal_injury" becomes an error instead of a silent bug.
"""

from __future__ import annotations

from enum import StrEnum


class PracticeArea(StrEnum):
    PERSONAL_INJURY = "personal_injury"
    MEDICAL_MALPRACTICE = "medical_malpractice"
    WORKERS_COMP = "workers_comp"
    EMPLOYMENT = "employment"
    FAMILY_LAW = "family_law"
    CRIMINAL_DEFENSE = "criminal_defense"
    BANKRUPTCY = "bankruptcy"
    IMMIGRATION = "immigration"
    ESTATE_PLANNING = "estate_planning"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title().replace("Workers Comp", "Workers' Comp")


class LeadStatus(StrEnum):
    RECEIVED = "received"  # just arrived, not checked yet
    REJECTED = "rejected"  # failed scrubbing (spam, duplicate, no consent...)
    UNSERVICEABLE = "unserviceable"  # real inquiry, but no participating attorney covers it
    OFFERED = "offered"  # waiting for an attorney to say yes
    ASSIGNED = "assigned"  # an attorney has it
    UNSOLD = "unsold"  # every attorney passed or ran out of time


class OfferStatus(StrEnum):
    QUEUED = "queued"  # ping tree: waiting its turn
    PENDING = "pending"  # attorney has been asked and the clock is running
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"  # clock ran out
    LOST = "lost"  # someone else claimed the lead first
    SKIPPED = "skipped"  # ping tree: no longer eligible when their turn came


class DistributionModel(StrEnum):
    EXCLUSIVE = "exclusive"  # one attorney owns this ZIP + practice area
    SHARED = "shared"  # several attorneys are asked at once; first to claim wins
    PING_TREE = "ping_tree"  # highest bidder asked first, then the next, and so on


class BillingPlan(StrEnum):
    PAY_PER_LEAD = "pay_per_lead"
    SUBSCRIPTION = "subscription"


class ChargeKind(StrEnum):
    """Every way money can be charged. There is deliberately no percentage kind.

    Charging a share of an attorney's fee would be fee-splitting with a
    non-lawyer, which bar rules forbid. Leaving it out of this list makes that
    mistake impossible to express, not just against policy.
    """

    PER_LEAD = "per_lead"  # flat price for one delivered lead
    INCLUDED = "included"  # lead delivered under a subscription or territory ($0)
    SUBSCRIPTION = "subscription"  # flat monthly plan fee
    TERRITORY = "territory"  # flat monthly fee for an exclusive ZIP + practice area
