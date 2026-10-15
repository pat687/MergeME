"""The shapes of data going in and out of the API.

Pydantic checks every field the moment a request arrives, so the rest of the
code can trust that a ZIP code really is five digits and an email really looks
like an email.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.domain import compliance
from app.domain.types import (
    BillingPlan,
    ChargeKind,
    DistributionModel,
    LeadStatus,
    OfferStatus,
    PracticeArea,
)

US_STATES = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "PR",
    }
)
# [0-9], not \d: \d also matches Arabic-Indic and full-width digits, which would slip
# through and then match no attorney's ZIP.
ZIP_RE = re.compile(r"^[0-9]{5}\Z")
ZIP_PLUS4_RE = re.compile(r"^([0-9]{5})(?:[- ]?[0-9]{4})?\Z")  # 60614, 60614-1234, 606141234
# Commas for "Smith, Jr."; zero-width (non-)joiners are part of how some scripts are typed.
NAME_PUNCTUATION = frozenset(" '-.,\u2019\u200c\u200d")
UTM_MAX = 120
NAME_MAX = 80  # matches the String(80) columns


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def _is_letter_or_mark(ch: str) -> bool:
    # Letters (L*) plus combining marks (M*): accents typed separately, and the vowel
    # signs of scripts such as Devanagari or Tamil, which str.isalpha() says aren't letters.
    return unicodedata.category(ch)[0] in "LM"


def _check_name(value: str) -> str:
    """Letters in any language plus spaces, hyphens, apostrophes, periods and commas.

    Blocks URLs, emails and digits, so no text a stranger types can turn an email we
    send into a convincing phishing message.
    """
    value = unicodedata.normalize("NFC", value)
    if len(value) > NAME_MAX:  # NFC can lengthen a few letters, so check again
        raise ValueError(f"must be {NAME_MAX} characters or fewer")
    if not any(ch.isalpha() for ch in value) or not all(
        _is_letter_or_mark(ch) or ch in NAME_PUNCTUATION for ch in value
    ):
        raise ValueError("use letters only (spaces, hyphens, apostrophes and periods are fine)")
    if re.search(r"\.[^\W\d_]", value):  # "J. R. Smith" is fine, "barrgo-login.net" isn't
        raise ValueError("a period must be followed by a space")
    return value


def clean_text(value: str) -> str:
    """Free text as stored: NFC form, and no control characters other than line breaks
    and tabs (Postgres can't store NUL, and the rest only hide text)."""
    value = unicodedata.normalize("NFC", value)
    return "".join(ch for ch in value if ch in "\n\r\t" or unicodedata.category(ch) != "Cc")


def _check_states(value: list[str]) -> list[str]:
    cleaned = [s.upper() for s in value]
    bad = [s for s in cleaned if s not in US_STATES]
    if bad:
        raise ValueError(f"unknown state codes: {bad}")
    return cleaned


def _check_service_zips(value: list[str]) -> list[str]:
    bad = [z for z in value if not re.fullmatch(r"[0-9]{3}|[0-9]{5}", z)]
    if bad:
        raise ValueError(f"service_zips must be 3 or 5 digits: {bad}")
    return value


def _check_bids(value: dict[PracticeArea, int]) -> dict[PracticeArea, int]:
    if any(cents < 0 for cents in value.values()):
        raise ValueError("bids must be zero or more cents")
    return value


# --------------------------------------------------------------------------- #
# Consumer intake
# --------------------------------------------------------------------------- #
class IntakeRequest(BaseModel):
    """What a person fills in on the intake form."""

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=NAME_MAX)
    last_name: str = Field(min_length=1, max_length=NAME_MAX)
    email: EmailStr
    phone: str = Field(description="US phone number, any common format")
    zip_code: str
    state: str = Field(description="Two-letter state code")
    practice_area: PracticeArea
    description: str = Field(min_length=10, max_length=2000)
    incident_date: date | None = None
    injured: bool | None = None
    medical_treatment: bool | None = None
    has_attorney: bool = False
    court_date_soon: bool = False
    consent: bool = Field(description="Agreed to be contacted by participating attorneys")
    consent_version: str = Field(min_length=1, max_length=40)
    sensitive_consent: bool = Field(
        default=False,
        description="Separate consent to send health or immigration details; required in "
        "personal injury, medical malpractice, workers' comp and immigration",
    )
    federal_or_maritime_worker: bool | None = Field(
        default=None,
        description="Workers' comp only, required there: a federal employee or a longshore, "
        "harbor or shipyard worker (those claims aren't accepted)",
    )
    # Honeypot: hidden from humans by CSS. Bots fill every field, people don't.
    website: str = ""
    # Where the inquiry came from, kept as evidence that it came from advertising the
    # person responded to (never from outreach to them). Cleaned and trimmed, never rejected.
    utm_source: str | None = Field(default=None, description=f"Trimmed to {UTM_MAX} characters")
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    referrer_host: str | None = None
    landing_path: str | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _names(cls, value: str) -> str:
        return _check_name(value)

    @field_validator(
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "referrer_host", "landing_path"
    )
    @classmethod
    def _trim_utm(cls, value: str | None) -> str | None:
        # Marketing tags come from links we don't control: clean and trim, never reject
        # the person. (A NUL from "%00" would otherwise make Postgres refuse the row.)
        cleaned = "".join(ch for ch in value or "" if unicodedata.category(ch)[0] != "C")
        return cleaned[:UTM_MAX] or None

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        return clean_text(value)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        digits = _digits(value)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError("phone number must have 10 digits")
        return digits

    @field_validator("zip_code")
    @classmethod
    def _check_zip(cls, value: str) -> str:
        match = ZIP_PLUS4_RE.match(value)
        if not match:
            raise ValueError("ZIP code must be 5 digits (or ZIP+4, like 60614-1234)")
        return match.group(1)

    @field_validator("state")
    @classmethod
    def _check_state(cls, value: str) -> str:
        value = value.upper()
        if value not in US_STATES:
            raise ValueError("state must be a two-letter US state code")
        return value

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _fit_the_practice_area(self) -> IntakeRequest:
        # Ask only what the matter needs: injury questions only in injury areas.
        if self.practice_area not in compliance.INJURY_AREAS:
            self.injured = self.medical_treatment = None
        if self.practice_area != PracticeArea.WORKERS_COMP:
            self.federal_or_maritime_worker = None
        elif self.federal_or_maritime_worker is None:
            raise ValueError(
                "please say whether you are a federal employee or a longshore, harbor or "
                "shipyard worker"
            )
        # Refused here, before anything is stored: health or immigration details may only
        # be collected with this separate consent.
        if self.practice_area in compliance.SENSITIVE_AREAS and not self.sensitive_consent:
            kind = compliance.SENSITIVE_AREAS[self.practice_area]
            raise ValueError(f"please agree to sending the {kind} details, or we can't help")
        return self


class IntakeResponse(BaseModel):
    lead_id: str | None = Field(description="Empty when nothing was kept (closed state)")
    status: LeadStatus
    message: str
    disclosure: str
    reload_form: bool = Field(
        default=False, description="True when the form's consent wording changed; reload it"
    )


# --------------------------------------------------------------------------- #
# Attorneys and territories (admin)
# --------------------------------------------------------------------------- #
class AttorneyBase(BaseModel):
    # extra="forbid" means a field like "fee_percent" is rejected outright,
    # which keeps percentage-of-fee pricing out of the system entirely.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    firm: str = Field(min_length=1, max_length=160)
    email: EmailStr
    phone: str | None = None
    bar_states: list[str] = Field(min_length=1)
    service_zips: list[str] = Field(
        min_length=1, description="5-digit ZIPs or 3-digit ZIP prefixes (e.g. '606')"
    )
    bids_cents: dict[PracticeArea, int] = Field(
        min_length=1, description="Flat price per lead, per practice area, in cents"
    )
    plan: BillingPlan = BillingPlan.PAY_PER_LEAD
    monthly_fee_cents: int = Field(default=0, ge=0)
    daily_cap: int = Field(default=10, ge=1, le=500)
    exo_username: str | None = None
    active: bool = True

    @field_validator("bar_states")
    @classmethod
    def _states(cls, value: list[str]) -> list[str]:
        return _check_states(value)

    @field_validator("service_zips")
    @classmethod
    def _zips(cls, value: list[str]) -> list[str]:
        return _check_service_zips(value)

    @field_validator("bids_cents")
    @classmethod
    def _bids(cls, value: dict[PracticeArea, int]) -> dict[PracticeArea, int]:
        return _check_bids(value)


class AttorneyCreate(AttorneyBase):
    pass


class AttorneyUpdate(BaseModel):
    """Partial update. Same rules as creating an attorney; only exo_username may be cleared."""

    model_config = ConfigDict(extra="forbid")

    active: bool | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=500)
    bids_cents: dict[PracticeArea, int] | None = Field(default=None, min_length=1)
    service_zips: list[str] | None = Field(default=None, min_length=1)
    exo_username: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nulls = sorted(k for k, v in data.items() if v is None and k != "exo_username")
            if nulls:
                raise ValueError(f"these fields cannot be null: {nulls}")
        return data

    @field_validator("service_zips")
    @classmethod
    def _zips(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_service_zips(value)

    @field_validator("bids_cents")
    @classmethod
    def _bids(cls, value: dict[PracticeArea, int] | None) -> dict[PracticeArea, int] | None:
        return None if value is None else _check_bids(value)


class AttorneyOut(AttorneyBase):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: int
    created_at: datetime
    license_numbers: dict[str, str] | None = None
    license_verified_at: datetime | None = None
    agreement_version: str | None = None
    agreement_accepted_at: datetime | None = None


class TerritoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attorney_id: int
    practice_area: PracticeArea
    zip_code: str
    monthly_fee_cents: int = Field(ge=0)

    @field_validator("zip_code")
    @classmethod
    def _check_zip(cls, value: str) -> str:
        if not ZIP_RE.match(value):
            raise ValueError("exclusive territories are sold per 5-digit ZIP")
        return value


class TerritoryOut(TerritoryCreate):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: int


# --------------------------------------------------------------------------- #
# Offers (attorney side)
# --------------------------------------------------------------------------- #
class OfferAction(BaseModel):
    token: str
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return clean_text(value) if value else value


class OfferView(BaseModel):
    """What an attorney sees. Contact details stay hidden until they accept."""

    offer_id: str
    status: OfferStatus
    model: DistributionModel
    practice_area: PracticeArea
    zip_code: str
    state: str
    score: int
    tier: str
    summary: str
    price_cents: int
    expires_at: datetime | None
    seconds_left: int | None = Field(
        default=None, description="Server-computed time left; clients count down from this"
    )
    contact: dict[str, Any] | None = None
    notice: str | None = None
    price_note: str | None = Field(default=None, description="How this offer's price was set")


# --------------------------------------------------------------------------- #
# Admin views
# --------------------------------------------------------------------------- #
class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attorney_id: int
    round: int
    status: OfferStatus
    price_cents: int
    created_at: datetime
    activated_at: datetime | None
    expires_at: datetime | None
    responded_at: datetime | None
    decline_reason: str | None = None
    link: str | None = None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: datetime
    event: str
    detail: dict[str, Any]


class LeadSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    status: LeadStatus
    practice_area: PracticeArea
    zip_code: str
    state: str
    score: int | None
    tier: str | None
    distribution_model: DistributionModel | None
    assigned_attorney_id: int | None


class LeadDetail(LeadSummary):
    first_name: str
    last_name: str
    email: str
    phone: str
    description: str
    rejection_reasons: list[str]
    score_breakdown: list[dict[str, Any]]
    offers: list[OfferOut]
    audit: list[AuditOut]


class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    kind: ChargeKind
    amount_cents: int
    period: str
    lead_id: str | None
    description: str


class Statement(BaseModel):
    attorney_id: int
    attorney_name: str
    period: str
    entries: list[LedgerEntryOut]
    total_cents: int


class BillingRunResult(BaseModel):
    period: str
    created: int
    skipped_existing: int
    not_billed: list[str] = Field(
        default_factory=list,
        description="Monthly fees held back because the attorney can't receive leads "
        "(license check or agreement); staff decide",
    )


# --------------------------------------------------------------------------- #
# The lead marketplace: attorneys set their own bids
# --------------------------------------------------------------------------- #
class BidRow(BaseModel):
    practice_area: PracticeArea
    label: str
    my_bid_cents: int | None = Field(description="Empty = not taking leads in this area")
    auctioned: bool = Field(description="True where leads are sold by auction (ping tree)")
    floor_cents: int = Field(description="Lowest bid accepted in this practice area")
    below_floor: bool = Field(description="Your bid is below the lowest accepted: no leads")
    competing_bidders: int = Field(
        description="Other active attorneys paying per lead who could get the same leads"
    )
    rank: int | None = Field(description="1 = first in line in the auction")
    to_be_first_cents: int | None = Field(
        description="A bid that would put you first in line, rounded up to a whole $10 "
        "so other attorneys' bids stay private. Empty when you're already first."
    )
    sold_last_30_days: int
    average_price_cents: int | None = Field(
        description="Rounded to $10, and only once at least 5 leads sold, so no single sale "
        "price (and so no rival's bid) can be worked out from it"
    )
    exclusive_territory: bool = Field(description="You rent an exclusive ZIP in this area")


class BidsView(BaseModel):
    attorney_id: int
    name: str
    firm: str
    plan: BillingPlan
    can_edit: bool = Field(description="False on a subscription: areas are set with staff")
    blocked: str | None = Field(default=None, description="Why you get no leads right now")
    pricing_rule: str
    increment_cents: int
    max_bid_cents: int
    areas: list[BidRow]


class BidsUpdate(BaseModel):
    """Only the areas listed change. A number sets the bid, null stops leads in that area."""

    model_config = ConfigDict(extra="forbid")

    bids_cents: dict[PracticeArea, StrictInt | None] = Field(min_length=1)


class BidLinkOut(BaseModel):
    sent_to: str
    link: str | None = Field(default=None, description="Shown only in demo mode")


class LicenseCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    license_numbers: dict[str, str] = Field(
        min_length=1,
        description='Bar number per state, as shown on the state bar site: {"IL": "6301234"}',
    )

    @field_validator("license_numbers")
    @classmethod
    def _numbers(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {k.strip().upper(): v.strip() for k, v in value.items()}
        bad = sorted(k for k in cleaned if k not in US_STATES)
        if bad:
            raise ValueError(f"not US state codes: {bad}")
        blank = sorted(k for k, v in cleaned.items() if not v or len(v) > 40)
        if blank:
            raise ValueError(f"give the bar number for {blank} (up to 40 characters)")
        return cleaned


class AgreementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=40)
    accepted_at: datetime | None = Field(default=None, description="Defaults to now")

    @field_validator("accepted_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("include a time zone, e.g. 2026-10-15T14:00:00Z")
        return value


class EraseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["narrative", "full"] = "narrative"
    reason: str = Field(min_length=3, max_length=200, description='e.g. "deletion request #12"')


class HoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool
    reason: str = Field(min_length=3, max_length=200)


class BidChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attorney_id: int
    at: datetime
    changes: dict[str, list[int | None]] = Field(description="Practice area: [before, after]")


class MarketBidder(BaseModel):
    attorney_id: int
    name: str
    bid_cents: int
    plan: BillingPlan
    blocked: str | None = Field(default=None, description="Why they get no leads right now")
    below_floor: bool = Field(description="Pays per lead but priced below the lowest bid: no leads")


class MarketArea(BaseModel):
    practice_area: PracticeArea
    label: str
    model: DistributionModel
    floor_cents: int
    bidders: list[MarketBidder]
    sold_last_30_days: int
    average_price_cents: int | None
