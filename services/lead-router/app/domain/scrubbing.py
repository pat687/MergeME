"""Step 2a: throw out junk before anyone sees it.

Like a mail room sorting out junk mail: no consent, obvious bots, fake phone
numbers and repeat submissions never reach an attorney. Each check returns a
plain-English reason so the decision is easy to explain later.

Everything here is a pure function (no database, no network) which makes it
fast and trivially easy to test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.compliance import OUTDATED_FORM
from app.schemas import IntakeRequest

DISPOSABLE_EMAIL_DOMAINS = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "temp-mail.org",
        "yopmail.com",
        "trashmail.com",
        "sharklasers.com",
        "getnada.com",
        "dispostable.com",
    }
)
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


@dataclass(frozen=True)
class ScrubResult:
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


def _phone_problem(phone: str) -> str | None:
    """North American numbers: area code and exchange can't start with 0 or 1."""
    area, exchange = phone[:3], phone[3:6]
    if area[0] in "01" or exchange[0] in "01":
        return "phone number is not a valid US number"
    if len(set(phone)) == 1:
        return "phone number is a repeated digit"
    return None


def scrub(
    inquiry: IntakeRequest, *, is_duplicate: bool, consent_version: str | None = None
) -> ScrubResult:
    """`consent_version` is the disclosure version currently shown on the form.

    The lead stores the server's consent wording, so the version the person agreed
    to must be that same version; otherwise the consent record wouldn't match.
    """
    reasons: list[str] = []

    if not inquiry.consent:
        reasons.append("no consent to be contacted")
    elif consent_version is not None and inquiry.consent_version != consent_version:
        reasons.append(OUTDATED_FORM)
    if inquiry.website:
        reasons.append("spam: hidden honeypot field was filled in")
    domain = inquiry.email.rsplit("@", 1)[-1]
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        reasons.append("disposable email address")
    if problem := _phone_problem(inquiry.phone):
        reasons.append(problem)
    if len(URL_RE.findall(inquiry.description)) >= 2:
        reasons.append("spam: description is mostly links")
    if len(set(inquiry.description.lower().split())) < 3:
        reasons.append("description too short to be useful")
    if is_duplicate:
        reasons.append("duplicate: same person and practice area submitted recently")

    return ScrubResult(reasons)
