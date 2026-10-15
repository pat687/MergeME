from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import IntakeRequest
from app.seed import seed
from app.services import links

ADMIN = {"X-Admin-Key": "test-admin"}
SECRET = "test-secret"
TODAY = date(2026, 10, 15)


def inquiry_data(**overrides: Any) -> dict[str, Any]:
    """A realistic, valid personal-injury inquiry from downtown Chicago."""
    data: dict[str, Any] = {
        "first_name": "Jamie",
        "last_name": "Rivera",
        "email": "jamie.rivera@example.org",
        "phone": "(312) 555-0142",
        "zip_code": "60601",
        "state": "IL",
        "practice_area": "personal_injury",
        "description": (
            "I was rear-ended at a red light on Michigan Avenue last week. My neck and back "
            "hurt and I went to urgent care the next morning. The other driver's insurance "
            "keeps calling me and I do not know what to say to them."
        ),
        "incident_date": "2026-10-08",
        "injured": True,
        "medical_treatment": True,
        "has_attorney": False,
        "court_date_soon": False,
        "consent": True,
        "consent_version": "2026-10-01",
    }
    data.update(overrides)
    return data


def inquiry(**overrides: Any) -> IntakeRequest:
    return IntakeRequest(**inquiry_data(**overrides))


def token(offer_id: str) -> str:
    return links.sign_offer(SECRET, offer_id)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        admin_api_key="test-admin",
        link_signing_secret=SECRET,
        embedded_worker=False,
        demo_mode=True,
        smtp_host=None,
        ping_offer_ttl_seconds=90,
        shared_offer_ttl_seconds=900,
    )


@pytest.fixture
def mailbox() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def app(settings: Settings, mailbox: list[dict[str, Any]]):
    application = create_app(settings, handlers={"email": mailbox.append})
    with application.state.session_factory() as db:
        seed(db)
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app):
    with app.state.session_factory() as session:
        yield session
