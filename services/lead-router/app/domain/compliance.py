"""Rules that keep the business on the right side of attorney ethics rules.

The short version (details and sources in docs/COMPLIANCE.md):

1. Tell people the truth: we're an advertising service, not a law firm, and
   attorneys pay to receive inquiries. We don't recommend anyone.
2. Charge attorneys flat fees only. Never a share of what they earn
   (that would be "fee-splitting").
3. Don't let the price depend on whether the person hires the attorney.
4. Keep proof of consent for every person we pass along.

This file holds the exact wording and the money guard so they live in one
reviewed place, not scattered around the code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.types import ChargeKind

REDACTED = "[redacted]"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]*)?\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}(?!\d)")
# "12 Elm St", "4500 N Lake Shore Drive". Capitalized words only, so everyday phrases
# like "3 blocks down the road" are left alone.
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z0-9][\w.'-]*\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Place|Pl"
    r"|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Circle|Cir)\b\.?"
)


class ComplianceError(ValueError):
    """Raised when code tries to record money in a way the rules don't allow."""


def redact(
    text: str,
    *,
    names: Iterable[str] = (),
    email: str | None = None,
    phone: str | None = None,
) -> str:
    """Best-effort removal of contact details from free text shown before an attorney accepts.

    Removes email addresses, US phone numbers, street addresses and the person's
    own name (when written with a capital letter, so a first name like "Will"
    doesn't erase the word "will").
    """
    out = text
    if email:
        out = re.sub(re.escape(email), REDACTED, out, flags=re.IGNORECASE)
    out = _EMAIL_RE.sub(REDACTED, out)
    out = _PHONE_RE.sub(REDACTED, out)
    if phone and len(phone) == 10:
        out = re.sub(rf"(?<!\d){phone[3:6]}[\s.-]*{phone[6:]}(?!\d)", REDACTED, out)
    out = _STREET_RE.sub(REDACTED, out)
    for name in names:
        for token in name.split():
            token = token.strip(".-'\u2019")
            if len(token) < 2:
                continue
            for form in {token.capitalize(), token.upper(), token[:1].upper() + token[1:]}:
                out = re.sub(rf"(?<!\w){re.escape(form)}(?!\w)", REDACTED, out)
    return out


def disclosure(platform_name: str) -> str:
    return (
        f"Attorney advertising. {platform_name} is not a law firm and does not give legal "
        "advice. Participating attorneys pay a flat fee to receive inquiries like yours. "
        "We do not recommend or endorse any attorney, and we have not evaluated your legal "
        "matter. Submitting this form does not create an attorney-client relationship."
    )


def consent_text(platform_name: str, max_attorneys: int) -> str:
    return (
        f"I agree that {platform_name} may share my inquiry with up to {max_attorneys} "
        "participating attorneys, and that those attorneys may contact me by phone, text or "
        "email about my inquiry, including by automated means. Consent is not a condition "
        "of any purchase. Message and data rates may apply."
    )


def assert_flat_fee(kind: ChargeKind | str, amount_cents: int) -> None:
    """The single gate every ledger entry passes through.

    Charging happens only when an attorney receives a lead or pays a flat monthly
    fee. There is no hook for "client hired the lawyer" or "case settled".
    """
    try:
        ChargeKind(kind)
    except ValueError:
        raise ComplianceError(f"charge kind {kind!r} is not allowed") from None
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
        raise ComplianceError("charges must be whole cents, not a ratio or percentage")
    if amount_cents < 0:
        raise ComplianceError("charges cannot be negative")


def consumer_message(status: str, platform_name: str) -> str:
    """What the person sees after submitting. Careful wording: no 'best lawyer' claims."""
    messages = {
        "offered": (
            "Thanks. Your inquiry has been sent to participating attorneys who handle this "
            "kind of matter in your area. If one is available, they will contact you directly."
        ),
        "assigned": (
            "Thanks. Your inquiry has been sent to a participating attorney who handles this "
            "kind of matter in your area. They will contact you directly."
        ),
        "unserviceable": (
            "Thanks. Right now no participating attorney covers this kind of matter in your "
            "area. Your state or local bar association runs a lawyer referral service that "
            "can help."
        ),
        "rejected": (
            "We couldn't accept this submission. Please check your details and try again, or "
            "contact your state or local bar association's lawyer referral service."
        ),
    }
    return messages.get(status, f"Thanks for contacting {platform_name}.")
