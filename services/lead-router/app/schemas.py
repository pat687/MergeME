"""The shapes of data going in and out of the API.

Pydantic checks every field the moment a request arrives, so the rest of the
code can trust that a ZIP code really is five digits and an email really looks
like an email.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

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
REASON_MAX = 200  # matches offers.decline_reason
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
    and tabs (Postgres can't store NUL, and the rest only hide text). Control characters
    that act as spacing (a Word soft line break, a page break) become a space, so the
    words around them don't run together."""
    value = unicodedata.normalize("NFC", value)
    return "".join(
        ch if ch in "\n\r\t" or unicodedata.category(ch) != "Cc" else (" " if ch.isspace() else "")
        for ch in value
    )


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
    # Honeypot: hidden from humans by CSS. Bots fill every field, people don't.
    website: str = ""
    utm_source: str | None = Field(default=None, description=f"Trimmed to {UTM_MAX} characters")

    @field_validator("first_name", "last_name")
    @classmethod
    def _names(cls, value: str) -> str:
        return _check_name(value)

    @field_validator("utm_source")
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


class IntakeResponse(BaseModel):
    lead_id: str
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
    reason: str | None = Field(default=None, max_length=REASON_MAX)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        # Trimmed after cleaning: NFC can lengthen a few letters past the column size.
        return clean_text(value)[:REASON_MAX] if value else value


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
