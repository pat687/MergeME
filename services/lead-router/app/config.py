"""Settings for the service, read from environment variables (or a .env file).

Plain-English version: this is the control panel. Every knob the business might
want to turn (how long an attorney has to answer, which practice areas use an
auction, where email goes) lives here, so nobody has to edit code to change it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain import compliance
from app.domain.types import PracticeArea
from app.schemas import US_STATES as US_STATE_CODES

DEV_ADMIN_KEY = "dev-admin-key-change-me"
DEV_SIGNING_SECRET = "dev-signing-secret-change-me"


class Settings(BaseSettings):
    # env_ignore_empty: docker-compose passes every setting through as ${VAR:-}; an empty
    # value then means "use the default" instead of failing to parse.
    model_config = SettingsConfigDict(
        env_prefix="ROUTER_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # --- Core -----------------------------------------------------------------
    database_url: str = "sqlite:///./lead_router.db"
    public_base_url: str = "http://localhost:8000"
    admin_api_key: str = DEV_ADMIN_KEY
    link_signing_secret: str = DEV_SIGNING_SECRET
    # Demo mode shows signed offer links in the admin API so you can click
    # through the attorney experience without opening an inbox. Never in prod.
    demo_mode: bool = False

    # --- Background worker ----------------------------------------------------
    # True: the API process also expires offers and sends email (simple, one box).
    # False: run `python -m app.worker` as its own process (what docker-compose does).
    embedded_worker: bool = True
    worker_interval_seconds: float = 2.0
    worker_heartbeat_file: str = "/tmp/lead-worker.heartbeat"
    outbox_max_attempts: int = 5
    # Stop sending after this long so a slow SMTP or eXo server can't delay offer timeouts.
    outbox_time_budget_seconds: float = 5.0

    # --- Where leads may be sold -----------------------------------------------
    # Nothing is sold in a state until it's listed here, ideally after a lawyer licensed
    # there approves it in writing. Inquiries from other states are refused before
    # anything is stored. JSON list, e.g. '["IL"]'.
    launch_states: list[str] = Field(default_factory=list)
    # Records counsel's written approval for a state ("IL") or one practice area in it
    # ("IL:personal_injury") that compliance.RESTRICTIONS flags. JSON list.
    counsel_cleared: list[str] = Field(default_factory=list)
    # Extra blocks: '{"NY": ["immigration"], "OR": ["*"]}'.
    blocked_areas_by_state: dict[str, list[str]] = Field(default_factory=dict)
    # Where counsel's written approval for the clearances above is filed, e.g. "Smith LLP
    # letter, 2026-11-02". Required outside demo mode when anything is cleared.
    counsel_approval_ref: str | None = None

    # --- Attorneys --------------------------------------------------------------
    # An attorney gets no leads unless staff recorded a bar-license check this recently
    # (POST /api/v1/admin/attorneys/{id}/license-check). Can be off only in demo mode.
    require_license_verification: bool = True
    license_recheck_days: int = Field(default=90, ge=1)
    # Once set, an attorney gets no leads until their acceptance of this version of the
    # participation agreement is recorded (POST /api/v1/admin/attorneys/{id}/agreement).
    attorney_agreement_version: str | None = None

    # --- Records ----------------------------------------------------------------
    # Days before personal details are erased (the worker does it; legal holds pause it).
    retention_rejected_days: int = Field(default=30, ge=1)  # rejected submissions
    retention_unrouted_days: int = Field(default=90, ge=1)  # unserviceable and unsold
    retention_narrative_days: int = Field(default=90, ge=1)  # delivered leads' story
    # Delivered leads keep the consent evidence (name, phone, email, consent text, time,
    # IP) this long for defending contact-law claims (4-year federal limit plus margin).
    retention_evidence_days: int = Field(default=1916, ge=1)
    retention_outbox_days: int = Field(default=14, ge=1)  # bodies of sent emails

    # --- Distribution rules ---------------------------------------------------
    # Which model each practice area uses when no one owns the territory
    # exclusively. "*" is the fallback for anything not listed.
    distribution_policy: dict[str, str] = Field(
        default_factory=lambda: {
            "personal_injury": "ping_tree",
            "medical_malpractice": "ping_tree",
            "workers_comp": "ping_tree",
            "*": "shared",
        }
    )
    # The most attorneys any one inquiry is shared with, in every model. It's also the
    # number written into the consent text, so the two always match.
    shared_max_attorneys: int = Field(default=3, ge=1)
    shared_offer_ttl_seconds: int = 15 * 60
    ping_offer_ttl_seconds: int = 90
    # Lowest price per lead (in cents) in each practice area: the lowest bid the auction
    # accepts, the lowest set price in shared areas, and what the last bidder in an
    # auction line pays. "*" covers the areas not listed. Every area needs one above $0.
    floor_price_cents: dict[str, int] = Field(
        default_factory=lambda: {
            "personal_injury": 5000,
            "medical_malpractice": 7500,
            "workers_comp": 4000,
            "*": 1500,
        }
    )
    # How the auction sets the price. "second_price": the winner pays one increment above
    # the next-highest bid (or the floor), never more than their own bid, so bidding your
    # true value is always safe. "first_price": the winner pays their own bid.
    auction_pricing: Literal["second_price", "first_price"] = "second_price"
    bid_increment_cents: int = Field(default=100, ge=0)
    # Highest bid an attorney can enter themselves; catches typos like $15,000.
    max_bid_cents: int = Field(default=500_000, ge=1)
    # How many times an attorney can save bid changes in 24 hours. A place in line answers
    # "is my bid above theirs?", so unlimited saves would let someone home in on a rival's
    # exact bid; every save is also recorded (bid_changes).
    max_bid_saves_per_day: int = Field(default=10, ge=1)
    duplicate_window_days: int = 30
    # Daily lead caps reset at midnight in this timezone, not at midnight UTC.
    business_timezone: str = "America/Chicago"
    # Per-IP limit on intake submissions (0 turns it off). Put a proxy limit in front too.
    intake_rate_limit_per_minute: int = 10

    # --- Email (SMTP) ---------------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 1025
    # The sending mailbox. A bare address gets the platform name as its display name, so
    # people see the same name in their inbox as in the consent text they agreed to.
    smtp_from: str = "info@legalleadrouter.com"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False  # port 587 style: connect plain, then upgrade
    smtp_ssl: bool = False  # port 465 style: encrypted from the start (Hostinger's default)

    # --- eXo Platform sync (optional) -----------------------------------------
    # When enabled, every lead is mirrored into the eXo "lead-capture" add-on so
    # the team can see the pipeline inside their eXo workspace.
    exo_sync_enabled: bool = False
    exo_base_url: str = "http://exo:8080"
    exo_leadcapture_token: str | None = None
    exo_username: str | None = None
    exo_password: str | None = None
    # Send consumers' names, contact details and descriptions to eXo too. Off: eXo gets
    # the reference, practice area, ZIP and status only, so the router stays the one
    # place personal data lives (and can be erased).
    exo_sync_contact_details: bool = False

    # --- Public wording -------------------------------------------------------
    platform_name: str = "Barrgo Legal Connect"
    disclosure_version: str = "2026-10-15"
    # The business behind the platform, shown in the disclosure, privacy policy and emails.
    # Required outside demo mode.
    legal_entity_name: str | None = None
    postal_address: str | None = None
    privacy_email: str = "info@legalleadrouter.com"
    # Where a state's rules call for a responsible lawyer to be named in advertising, the
    # line to show for that state (counsel decides): '{"IL": "Jane Doe, Doe Law LLC, Chicago"}'.
    responsible_attorneys: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        # A missing or $0 floor would quietly give leads away: the last bidder in an
        # auction pays the floor. Fail at startup instead.
        free = [area.value for area in PracticeArea if self.floor_for(area) < 1]
        if free:
            raise ValueError(
                "ROUTER_FLOOR_PRICE_CENTS needs a price above 0 for every practice area "
                f'(missing or 0: {", ".join(free)}). Add a "*" entry to cover the rest, '
                'for example {"personal_injury": 5000, "*": 1500}.'
            )
        unbiddable = [
            area.value for area in PracticeArea if self.floor_for(area) > self.max_bid_cents
        ]
        if unbiddable:
            raise ValueError(
                f"ROUTER_FLOOR_PRICE_CENTS is above ROUTER_MAX_BID_CENTS ({self.max_bid_cents}) "
                f"for {', '.join(unbiddable)}, so nobody could bid there."
            )
        self._check_states()
        if not self.demo_mode:
            self._check_production()
        return self

    def _check_states(self) -> None:
        unknown = sorted(
            {s for s in self.launch_states if s not in US_STATE_CODES}
            | {s for s in self.blocked_areas_by_state if s not in US_STATE_CODES}
            | {
                f"{state}:{area}"
                for state, areas in self.blocked_areas_by_state.items()
                for area in areas
                if area != "*" and area not in set(PracticeArea)
            }
            | {s for s in self.responsible_attorneys if s not in US_STATE_CODES}
            | {
                entry
                for entry in self.counsel_cleared
                if entry.split(":")[0] not in US_STATE_CODES
                or (":" in entry and entry.split(":", 1)[1] not in set(PracticeArea))
            }
        )
        if unknown:
            raise ValueError(
                f"unknown state or practice area in the launch settings: {', '.join(unknown)}"
            )
        uncleared = [
            f"{state} ({compliance.RESTRICTIONS[state]['*']})"
            for state in self.launch_states
            if "*" in compliance.RESTRICTIONS.get(state, {}) and state not in self.counsel_cleared
        ]
        if uncleared:
            raise ValueError(
                "ROUTER_LAUNCH_STATES includes states with known restrictions: "
                + "; ".join(uncleared)
                + ". Add the state to ROUTER_COUNSEL_CLEARED only after a lawyer licensed "
                "there approves selling leads in writing."
            )

    def _check_production(self) -> None:
        from urllib.parse import urlparse

        problems = []
        url = urlparse(self.public_base_url)
        # Links in emails go to people outside, so not even localhost will do.
        if url.scheme != "https" or url.hostname in {"localhost", "127.0.0.1"}:
            problems.append("ROUTER_PUBLIC_BASE_URL must be your public https:// address")
        if self.uses_dev_secrets:
            problems.append("set ROUTER_ADMIN_API_KEY and ROUTER_LINK_SIGNING_SECRET (make init)")
        if self.counsel_cleared and not self.counsel_approval_ref:
            problems.append(
                "ROUTER_COUNSEL_CLEARED is set: record where counsel's written approval is "
                "filed in ROUTER_COUNSEL_APPROVAL_REF (the demo values in .env.example are not "
                "an approval)"
            )
        if (self.smtp_host or "").lower() == "mailpit":
            problems.append("ROUTER_SMTP_HOST is the Mailpit test inbox; use a real mail server")
        if not self.require_license_verification:
            problems.append("ROUTER_REQUIRE_LICENSE_VERIFICATION can be false only in demo mode")
        if not self.legal_entity_name or not self.postal_address:
            problems.append("set ROUTER_LEGAL_ENTITY_NAME and ROUTER_POSTAL_ADDRESS")
        if problems:
            raise ValueError(
                "not ready for production (ROUTER_DEMO_MODE=false): " + "; ".join(problems)
            )

    @property
    def operator(self) -> str | None:
        """'Example LLC, 1 Main St, Chicago, IL 60601', for disclosures and emails."""
        if not self.legal_entity_name:
            return None
        return ", ".join(p for p in (self.legal_entity_name, self.postal_address) if p)

    def closed_reason(self, state: str, practice_area: str) -> str | None:
        return compliance.closed_reason(
            state,
            practice_area,
            launch_states=self.launch_states,
            cleared=self.counsel_cleared,
            blocked=self.blocked_areas_by_state,
        )

    def policy_for(self, practice_area: str) -> str:
        return self.distribution_policy.get(
            practice_area, self.distribution_policy.get("*", "shared")
        )

    def floor_for(self, practice_area: str) -> int:
        return self.floor_price_cents.get(practice_area, self.floor_price_cents.get("*", 0))

    @property
    def uses_dev_secrets(self) -> bool:
        return self.admin_api_key == DEV_ADMIN_KEY or self.link_signing_secret == DEV_SIGNING_SECRET


@lru_cache
def get_settings() -> Settings:
    return Settings()
