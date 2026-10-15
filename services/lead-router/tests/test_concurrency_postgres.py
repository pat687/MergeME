"""Real concurrency tests: many threads hit the same lead at the same instant.

These need Postgres (SQLite serializes writers, so it can't show a race).
They run in CI against a Postgres service and are skipped locally unless
TEST_POSTGRES_URL is set, e.g.

    TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres pytest
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.config import Settings
from app.db import Base, make_engine, make_session_factory, utcnow
from app.domain.types import LeadStatus, OfferStatus
from app.models import Attorney, Lead, LedgerEntry, Offer
from app.seed import seed
from app.services import offers, pipeline
from tests.conftest import SECRET, inquiry, token

PG_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set TEST_POSTGRES_URL to run")

FAMILY = {"practice_area": "family_law", "injured": None, "medical_treatment": None}


@pytest.fixture
def pg():
    settings = Settings(
        _env_file=None, database_url=PG_URL, link_signing_secret=SECRET, embedded_worker=False
    )
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        seed(db)
    yield settings, factory
    engine.dispose()


def all_at_once(n: int, work: Callable[[int], object]) -> list[object]:
    """Run work(i) in n threads released by one barrier, so they really collide."""
    barrier = threading.Barrier(n)

    def run(i: int) -> object:
        barrier.wait()
        try:
            return work(i)
        except offers.OfferError as err:
            return err

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(run, range(n)))


def new_lead(settings, factory, n: int = 0, **overrides) -> Lead:
    data = {"email": f"race{n}@example.org", "phone": f"312555{2000 + n}", **overrides}
    with factory() as db:
        return pipeline.process_intake(
            db, settings, inquiry(**data), pipeline.RequestMeta(), utcnow()
        )


def test_simultaneous_accepts_have_exactly_one_winner(pg):
    settings, factory = pg
    for round_ in range(5):
        lead = new_lead(settings, factory, round_, **FAMILY)
        with factory() as db:
            offer_ids = [o.id for o in db.query(Offer).filter_by(lead_id=lead.id)]

        def accept(i: int, ids=offer_ids):
            with factory() as db:
                return offers.accept(db, settings, ids[i], token(ids[i]), utcnow())

        results = all_at_once(len(offer_ids), accept)
        winners = [r for r in results if isinstance(r, Offer)]
        losers = [r for r in results if isinstance(r, offers.OfferError)]
        assert len(winners) == 1 and len(losers) == len(offer_ids) - 1
        assert all(e.status_code == 409 for e in losers)
        with factory() as db:
            assert db.get(Lead, lead.id).assigned_attorney_id == winners[0].attorney_id
            assert db.query(LedgerEntry).filter_by(lead_id=lead.id).count() == 1


def test_identical_simultaneous_intakes_are_sold_once(pg):
    settings, factory = pg

    def submit(_i: int):
        with factory() as db:
            lead = pipeline.process_intake(
                db, settings, inquiry(**FAMILY), pipeline.RequestMeta(), utcnow()
            )
            return lead.status

    statuses = all_at_once(4, submit)
    assert sorted(statuses).count(LeadStatus.REJECTED) == 3
    assert [s for s in statuses if s != LeadStatus.REJECTED] == [LeadStatus.OFFERED]


def test_daily_cap_holds_when_accepts_race(pg):
    settings, factory = pg
    with factory() as db:
        riley = db.query(Attorney).filter_by(name="Riley Chen").one()
        riley.daily_cap = 1
        db.commit()
        riley_id = riley.id
    leads = [new_lead(settings, factory, n, **FAMILY) for n in range(3)]
    with factory() as db:
        riley_offers = [
            db.query(Offer).filter_by(lead_id=lead.id, attorney_id=riley_id).one().id
            for lead in leads
        ]

    def accept(i: int):
        with factory() as db:
            return offers.accept(db, settings, riley_offers[i], token(riley_offers[i]), utcnow())

    results = all_at_once(3, accept)
    assert sum(isinstance(r, Offer) for r in results) == 1
    with factory() as db:
        assert db.query(LedgerEntry).filter_by(attorney_id=riley_id).count() == 1


def test_accept_racing_the_expiry_worker_stays_consistent(pg):
    settings, factory = pg
    for n in range(10):
        lead = new_lead(settings, factory, n)  # personal injury -> ping tree, Avery pending
        with factory() as db:
            first = db.query(Offer).filter_by(lead_id=lead.id, round=1).one().id
        late = utcnow() + timedelta(seconds=settings.ping_offer_ttl_seconds + 1)

        def work(i: int, offer_id=first, later=late):
            with factory() as db:
                if i == 0:
                    return offers.accept(db, settings, offer_id, token(offer_id), utcnow())
                return offers.expire_due(db, settings, later)

        all_at_once(2, work)
        with factory() as db:
            state = db.get(Lead, lead.id)
            rows = {o.round: o.status for o in db.query(Offer).filter_by(lead_id=lead.id)}
            accepted = [r for r, s in rows.items() if s == OfferStatus.ACCEPTED]
            # Either the accept won (round 1 accepted, lead assigned) or the timeout won
            # (round 1 expired, round 2 now pending). Never both, never neither.
            if rows[1] == OfferStatus.ACCEPTED:
                assert state.status == LeadStatus.ASSIGNED and accepted == [1]
            else:
                assert rows[1] == OfferStatus.EXPIRED and rows[2] == OfferStatus.PENDING
                assert state.status == LeadStatus.OFFERED and not accepted


def test_router_and_worker_can_upgrade_the_schema_at_the_same_time(pg):
    from sqlalchemy import inspect, text

    from app.db import ADDED_COLUMNS, upgrade_schema

    engine = make_engine(PG_URL)
    with engine.begin() as conn:
        for table, column, _type in ADDED_COLUMNS:  # an older database
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    all_at_once(4, lambda _i: upgrade_schema(make_engine(PG_URL)))  # raises if any collide
    for table, column, _type in ADDED_COLUMNS:
        assert column in {c["name"] for c in inspect(engine).get_columns(table)}
    engine.dispose()
