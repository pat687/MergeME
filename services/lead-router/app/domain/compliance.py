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
_SEP = r"[\s./\-\u2010-\u2015]*"  # spaces, dots, slashes, hyphens and Unicode dashes
_PHONE_RE = re.compile(
    rf"(?<![0-9])(?:\+?1{_SEP})?\(?[0-9]{{3}}\)?{_SEP}[0-9]{{3}}{_SEP}[0-9]{{4}}(?![0-9])"
)
# Between the digits of the person's own number: any mix of up to 3 separators.
_DIGIT_GAP = r"[\s()./+\-\u2010-\u2015]{0,3}"
# "12 Elm St", "4500 N Lake Shore Drive": a number, at least one capitalized word, a suffix.
# Requiring the word keeps "6 Dr appointments" (doctor) intact.
_STREET_CAPS_RE = re.compile(
    r"\b[0-9]{1,6}\s+((?:[A-Z0-9][\w.'-]*\s+){1,4})"
    r"(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Place|Pl"
    r"|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Circle|Cir)\b\.?"
)
# "In 2019 Family Court ..." is a courtroom, not an address.
_COURT_WORDS = frozenset(
    "family divorce circuit county district superior supreme juvenile probate traffic small "
    "claims municipal housing appeals appellate bankruptcy federal state criminal civil drug "
    "domestic relations tax chancery magistrate justice city".split()
)
# "I live at 4417 Kedzie": no suffix, but the lead-in makes it an address.
_LIVES_AT_RE = re.compile(
    r"(?i:\b((?:(?:live|lives|living|reside|resides|residing|stay|stays|staying)\s+(?:at|on)"
    r"|address\s+is|address:)\s+))"
    r"[0-9]{1,6}(?:\s+(?:[NSEWnsew]\.?(?!\w)|[A-Z][\w'.-]*)){1,3}"
)
# "12 elm st": lowercase only with unambiguous suffixes, and never across everyday words,
# so "3 blocks down the street" or "2 cars on the way" are left alone.
_STREET_LOWER_RE = re.compile(
    r"\b[0-9]{1,6}\s+((?:[a-z0-9][\w.'-]*\s+){1,3})"
    r"(?:st|street|ave|avenue|rd|blvd|boulevard|ln|ct|hwy|pkwy)\b\.?"
)
_FILLER = frozenset(
    "the a an my our your his her their this that down up on of from to in at near by across "
    "over past along blocks block miles mile feet foot cars car minutes minute times houses "
    "doors people kids".split()
)
# Names that are also everyday words: only redacted when written with a capital letter.
_COMMON_WORDS = frozenset(
    "will may mark rose hope bill grant chase lane june april august art dawn faith joy pat "
    "sue ray jack guy rich frank sky summer autumn winter spring amber ruby crystal ivy holly "
    "iris lily daisy violet rock stone hunter carter mason cash king young brown white black "
    "green long short small little wise love man page reed wood woods hill hall field ford "
    "bell cook baker price rice bush case park banks rivers river story star storm drew gene "
    "gale jean sandy rusty rocky bud buck chip chuck don dean earl major judge justice liberty "
    "harmony melody destiny trinity honey candy angel baby ace max sunny penny glen dale".split()
)


def _redact_lower_street(match: re.Match) -> str:
    words = match.group(1).lower().split()
    return match.group(0) if any(w in _FILLER for w in words) else REDACTED


def _redact_caps_street(match: re.Match) -> str:
    words = match.group(1).lower().split()
    is_court = match.group(2) in ("Court", "Ct") and any(w in _COURT_WORDS for w in words)
    return match.group(0) if is_court else REDACTED


def _name_parts(name: str) -> list[str]:
    """'Garcia-Lopez' -> ['Garcia-Lopez', 'Garcia', 'Lopez'], longest first."""
    parts: set[str] = set()
    for token in name.split():
        for piece in [token, *re.split(r"[-'\u2019]", token)]:
            piece = piece.strip(".,-'\u2019")
            if len(piece) >= 2:
                parts.add(piece)
    return sorted(parts, key=len, reverse=True)


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

    Removes email addresses, US phone numbers (any common separators), street
    addresses and the person's own name in any capitalization, including each part
    of a hyphenated name. Names that are also everyday words ("Will", "Grant") are
    only removed when capitalized, so the word "will" survives.
    """
    out = text
    if email:
        out = re.sub(re.escape(email), REDACTED, out, flags=re.IGNORECASE)
    out = _EMAIL_RE.sub(REDACTED, out)
    out = _PHONE_RE.sub(REDACTED, out)
    if phone and len(phone) == 10 and phone.isdigit():
        # Their own number however it's written: "3 1 2 5 5 5 ...", "555/0142".
        for digits in (phone, phone[3:]):
            joined = _DIGIT_GAP.join(digits)
            out = re.sub(rf"(?<![0-9])(?:1{_DIGIT_GAP})?{joined}(?![0-9])", REDACTED, out)
    out = _STREET_CAPS_RE.sub(_redact_caps_street, out)
    out = _STREET_LOWER_RE.sub(_redact_lower_street, out)
    out = _LIVES_AT_RE.sub(lambda m: m.group(1) + REDACTED, out)
    for name in names:
        for token in _name_parts(name):
            if len(token) >= 3 and token.lower() not in _COMMON_WORDS:
                out = re.sub(
                    rf"(?<!\w){re.escape(token)}(?!\w)", REDACTED, out, flags=re.IGNORECASE
                )
            else:
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


OUTDATED_FORM = "consent was given on an outdated form; please reload and try again"


def consumer_message(status: str, platform_name: str, reasons: Iterable[str] = ()) -> str:
    """What the person sees after submitting. Careful wording: no 'best lawyer' claims."""
    if status == "rejected" and OUTDATED_FORM in reasons:
        return (
            "This form was updated while you had it open. Please review the consent wording "
            "and submit again."
        )
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
