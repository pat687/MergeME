"""The shapes of data going in and out of the API.

Pydantic checks every field the moment a request arrives, so the rest of the
code can trust that a ZIP code really is five digits and an email really looks
like an email.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

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
ZIP_RE = re.compile(r"^\d{5}$")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


# --------------------------------------------------------------------------- #
# Consumer intake
# --------------------------------------------------------------------------- #
class IntakeRequest(BaseModel):
    """What a person fills in on the intake form."""

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
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
    consent_version: str
    # Honeypot: hidden from humans by CSS. Bots fill every field, people don't.
    website: str = ""
    utm_source: str | None = Field(default=None, max_length=120)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        digits = _digits(value)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError("phone must have 10 digits")
        return digits

    @field_validator("zip_code")
    @classmethod
    def _check_zip(cls, value: str) -> str:
        value = value[:5]
        if not ZIP_RE.match(value):
            raise ValueError("zip_code must be 5 digits")
        return value

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
    def _check_states(cls, value: list[str]) -> list[str]:
        cleaned = [s.upper() for s in value]
        bad = [s for s in cleaned if s not in US_STATES]
        if bad:
            raise ValueError(f"unknown state codes: {bad}")
        return cleaned

    @field_validator("service_zips")
    @classmethod
    def _check_zips(cls, value: list[str]) -> list[str]:
        bad = [z for z in value if not re.fullmatch(r"\d{3}|\d{5}", z)]
        if bad:
            raise ValueError(f"service_zips must be 3 or 5 digits: {bad}")
        return value

    @field_validator("bids_cents")
    @classmethod
    def _check_bids(cls, value: dict[PracticeArea, int]) -> dict[PracticeArea, int]:
        if any(cents < 0 for cents in value.values()):
            raise ValueError("bids must be zero or more cents")
        return value


class AttorneyCreate(AttorneyBase):
    pass


class AttorneyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=500)
    bids_cents: dict[PracticeArea, int] | None = None
    service_zips: list[str] | None = None
    exo_username: str | None = None


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
    reason: str | None = Field(default=None, max_length=200)


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
