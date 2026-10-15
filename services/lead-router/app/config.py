"""Settings for the service, read from environment variables (or a .env file).

Plain-English version: this is the control panel. Every knob the business might
want to turn (how long an attorney has to answer, which practice areas use an
auction, where email goes) lives here, so nobody has to edit code to change it.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_ADMIN_KEY = "dev-admin-key-change-me"
DEV_SIGNING_SECRET = "dev-signing-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTER_", env_file=".env", extra="ignore")

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
    outbox_max_attempts: int = 5

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
    shared_max_attorneys: int = 3
    shared_offer_ttl_seconds: int = 15 * 60
    ping_offer_ttl_seconds: int = 90
    # Lowest bid (in cents) the auction will accept per practice area.
    floor_price_cents: dict[str, int] = Field(
        default_factory=lambda: {
            "personal_injury": 5000,
            "medical_malpractice": 7500,
            "workers_comp": 4000,
            "*": 1500,
        }
    )
    duplicate_window_days: int = 30

    # --- Email (SMTP) ---------------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 1025
    smtp_from: str = "Barrgo Leads <leads@barrgo.local>"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False

    # --- eXo Platform sync (optional) -----------------------------------------
    # When enabled, every lead is mirrored into the eXo "lead-capture" add-on so
    # the team can see the pipeline inside their eXo workspace.
    exo_sync_enabled: bool = False
    exo_base_url: str = "http://exo:8080"
    exo_leadcapture_token: str | None = None
    exo_username: str | None = None
    exo_password: str | None = None

    # --- Public wording -------------------------------------------------------
    platform_name: str = "Barrgo Legal Connect"
    disclosure_version: str = "2026-10-01"

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
