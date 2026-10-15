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
import unicodedata
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
# "In 2019 Family Court ..." is a courtroom, not an address: a year, then a court's name
# ending in "Court" (never the "Ct" abbreviation, which is a street).
_COURT_WORDS = frozenset(
    "family divorce circuit county district superior supreme juvenile probate traffic small "
    "claims municipal housing appeals appellate bankruptcy federal state criminal civil drug "
    "domestic relations tax chancery magistrate justice city immigration common pleas "
    "surrogate surrogates orphans".split()
)
_YEAR_RE = re.compile(r"(?:19|20)[0-9]{2}\s")
# "I live at 4417 Kedzie", "lived at 918 n albany", "address is: 88 Wabash": no suffix,
# but the lead-in makes it an address. A lowercase street name is only taken after a
# direction letter, so "lived at 2 different places" is left alone.
_LIVES_AT_RE = re.compile(
    r"(?i:\b((?:(?:live|lives|lived|living|reside|resides|resided|residing"
    r"|stay|stays|stayed|staying)\s+(?:at|on)|address(?:\s+is)?:?)\s+))"
    r"[0-9]{1,6}(?:\s+[NSEWnsew]\.?(?!\w)(?:\s+[a-z][\w'.-]*)?|\s+[A-Z][\w'.-]*){1,3}"
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
    words = [w.removesuffix("'s").rstrip("'\u2019") for w in match.group(1).lower().split()]
    is_court = (
        match.group(2) == "Court"
        and _YEAR_RE.match(match.group(0)) is not None
        and words[-1] in _COURT_WORDS
    )
    return match.group(0) if is_court else REDACTED


def _name_parts(name: str) -> list[str]:
    """'Garcia-Lopez' -> ['Garcia-Lopez', 'Garcia', 'Lopez'], longest first."""
    parts: set[str] = set()
    for token in re.split(r"[\s,]+", name):
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
    # Same Unicode form on both sides, or a name typed one way in the form and another
    # way in the description (e.g. a separately typed accent) wouldn't match.
    out = unicodedata.normalize("NFC", text)
    names = [unicodedata.normalize("NFC", n) for n in names]
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


def disclosure(platform_name: str, operator: str | None = None) -> str:
    """Shown above the form and in the receipt email. Changing it means bumping
    ROUTER_DISCLOSURE_VERSION (docs/COMPLIANCE.md, "Changing the wording safely")."""
    text = (
        f"Attorney advertising. {platform_name} is an advertising service paid for by "
        "participating attorneys. It is not a law firm, does not give legal advice, and no "
        f"one at {platform_name} reviews your matter. We send your inquiry only to "
        "participating attorneys who are licensed in your state, handle the type of matter "
        "you chose and serve your ZIP code. When more than one qualifies, attorneys who have "
        "had fewer inquiries that day or who pay more per inquiry may be offered it first, "
        "and in some areas one attorney pays a monthly fee to receive every inquiry for a "
        "ZIP code. Attorneys pay us per inquiry or monthly. The amount never depends on "
        "whether you hire them or how your matter turns out, and it is never a share of a "
        "legal fee. We do not recommend or endorse any attorney. Submitting this form does "
        "not create an attorney-client relationship, and an attorney who sees your inquiry "
        "may already represent someone else in your matter."
    )
    return f"{text} {platform_name} is operated by {operator}." if operator else text


# Shown on the form until a valid phone number is typed, then replaced by that number,
# which is also what the stored consent says.
PHONE_PLACEHOLDER = "the phone number I entered"


def phone_display(digits: str) -> str:
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def consent_text(platform_name: str, max_attorneys: int, phone: str | None = None) -> str:
    """The consumer's instruction and permission to be contacted. Written as a request
    ("Please send my inquiry...") because the consumer is directing the disclosure."""
    number = phone_display(phone) if phone else PHONE_PLACEHOLDER
    return (
        f"Please send my inquiry to up to {max_attorneys} participating attorneys who "
        f"handle this type of matter in my area. I understand {platform_name} is paid by "
        "those attorneys. I agree that they may contact me about my inquiry by phone call or "
        f"text message to {number}, including with automated technology, and by email. I am "
        "18 or older, and the phone number and email I entered are mine or ones I regularly "
        "use. My consent is not a condition of buying anything. Message and data rates may "
        "apply. I can stop contact at any time by telling the attorney or replying STOP to "
        "a text."
    )


# Practice areas whose inquiries carry health or immigration information. Those need a
# separate, specific consent under several state privacy laws (docs/COMPLIANCE.md).
SENSITIVE_AREAS = {
    "personal_injury": "health",
    "medical_malpractice": "health",
    "workers_comp": "health",
    "immigration": "immigration",
}
INJURY_AREAS = frozenset(a for a, kind in SENSITIVE_AREAS.items() if kind == "health")


def sensitive_consent_text(platform_name: str, practice_area: str) -> str | None:
    kind = SENSITIVE_AREAS.get(practice_area)
    if kind == "health":
        return (
            f"I agree that {platform_name} may collect the health information I give on this "
            "form, such as an injury or medical treatment, and send it, for a fee paid by the "
            "attorneys, to the participating attorneys described above so they can contact me "
            "about my matter."
        )
    if kind == "immigration":
        return (
            f"I agree that {platform_name} may collect the information about my immigration "
            "matter that I give on this form and send it, for a fee paid by the attorneys, to "
            "the participating attorneys described above so they can contact me about it."
        )
    return None


def area_notices(platform_name: str) -> dict[str, str]:
    """Extra statements shown when a practice area is chosen."""
    return {
        "immigration": (
            f"No one at {platform_name} is an attorney or an accredited representative. We "
            "can't help with immigration forms or give advice; we only pass your inquiry to "
            "participating attorneys."
        ),
        # The 11 U.S.C. 528 statement, for the participating attorneys.
        "bankruptcy": (
            "Participating attorneys are debt relief agencies. They help people file for "
            "bankruptcy relief under the Bankruptcy Code."
        ),
        "workers_comp": (
            "Claims by federal government employees and by longshore, harbor or shipyard "
            "workers follow federal programs, so we can't accept them here."
        ),
    }


# --------------------------------------------------------------------------- #
# Where leads may be sold
# --------------------------------------------------------------------------- #
# Known reasons a state, or a practice area in a state, needs a lawyer's written approval
# before leads are sold there. From a 2026-10 legal audit whose sources could not be
# checked against the official texts, so treat each line as a question for counsel, not
# an answer. Leads covered here are never sold unless ROUTER_COUNSEL_CLEARED lists
# "ST" (the whole state) or "ST:practice_area". docs/legal/COUNSEL_BRIEF.md has the detail.
_INJURY = ("personal_injury", "medical_malpractice", "workers_comp")
RESTRICTIONS: dict[str, dict[str, str]] = {
    "CA": {
        "*": "lawyer referral services must be certified by the State Bar (Bus. & Prof. "
        "Code 6155; Jackson v. LegalMatch.com); SB 37 (2025); workers' comp and "
        "insurance anti-referral laws (Lab. Code 3215, Ins. Code 750)"
    },
    "TX": {
        "*": "barratry (Penal Code 38.12); for-profit referral services can't be certified "
        "(Occ. Code ch. 952); per-lead pricing outside Ethics Op. 573"
    },
    "CO": {"*": "SB26-174 (2026) makes paying for legal leads a deceptive trade practice"},
    "MI": {"*": "per-lead fees treated as paying for a recommendation (Informal Op. RI-365)"},
    "OH": {
        "*": "referral services must be nonprofit or registered (Gov.Bar R. XVI; Adv. Op. 2016-3)"
    },
    "FL": {
        "*": "'qualifying provider' filings and conditions (Rule 4-7.22); Fla. Stat. "
        "877.02 and 440.105"
    },
    "LA": {"*": "criminal ban on paying to obtain representation (La. R.S. 37:219)"},
    "GA": {"*": "referral-service filing rules (Ga. RPC 7.3(c)), amended March 2026"},
    "NJ": {
        "*": "for-profit referral services barred (RPC 7.3(d)); per-lead pricing "
        "scrutiny (CAA Op. 43; Joint Op. 47/741)"
    },
    "KY": {"*": "for-profit referral services barred by older opinions (KBA E-313, E-334, E-344)"},
    "SC": {"*": "services that limit which lawyers can join may violate Rule 7.2(c) (EAO 01-03)"},
    "IL": dict.fromkeys(_INJURY, "Insurance Claims Fraud Prevention Act (740 ILCS 92/5)"),
    "PA": dict.fromkeys(_INJURY, "paying for referrals of insurance claims (18 Pa.C.S. 4117(b))"),
    "WA": dict.fromkeys(
        _INJURY,
        "selling consumer health data needs a signed authorization "
        "naming the buyer (My Health My Data Act, RCW 19.373.070)",
    ),
    "NV": dict.fromkeys(
        _INJURY, "selling consumer health data needs written authorization (SB 370, NRS 603A)"
    ),
    "CT": dict.fromkeys(
        (*_INJURY, "immigration"),
        "consent rules for selling sensitive and consumer health data (CTDPA)",
    ),
    "MD": dict.fromkeys(
        (*_INJURY, "immigration"),
        "selling sensitive data is banned (Maryland Online Data Privacy Act)",
    ),
    "NC": {"workers_comp": "workers' comp solicitation and fee rules (N.C.G.S. 97-90)"},
    "DC": {"workers_comp": "workers' comp solicitation ban (D.C. Code 32-1530(e))"},
}


def restriction(state: str, practice_area: str, cleared: Iterable[str]) -> str | None:
    """Why leads for this state and area need counsel's approval, unless it's recorded."""
    cleared = set(cleared)
    rules = RESTRICTIONS.get(state, {})
    if "*" in rules and state not in cleared:
        return rules["*"]
    if (
        practice_area in rules
        and state not in cleared
        and (f"{state}:{practice_area}" not in cleared)
    ):
        return rules[practice_area]
    return None


def closed_reason(
    state: str,
    practice_area: str,
    *,
    launch_states: Iterable[str],
    cleared: Iterable[str],
    blocked: dict[str, list[str]],
) -> str | None:
    """Why an inquiry from this state in this practice area can't be accepted, or None."""
    if state not in set(launch_states):
        return "not open in this state"
    off = blocked.get(state, [])
    if "*" in off or practice_area in off:
        return "turned off for this state"
    return restriction(state, practice_area, cleared)


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
        "closed": (
            "Sorry, we can't accept this type of inquiry from your state, so we didn't keep "
            "what you entered. Your state or local bar association runs a lawyer referral "
            "service that can help."
        ),
        "federal_worker": (
            "Sorry, we can't accept this inquiry, so we didn't keep what you entered. Claims "
            "by federal employees and by longshore, harbor or shipyard workers follow federal "
            "programs: the U.S. Department of Labor's Office of Workers' Compensation "
            "Programs (dol.gov/owcp) and your state or local bar association's lawyer "
            "referral service can help."
        ),
        "rejected": (
            "We couldn't accept this submission. Please check your details and try again, or "
            "contact your state or local bar association's lawyer referral service."
        ),
    }
    return messages.get(status, f"Thanks for contacting {platform_name}.")
