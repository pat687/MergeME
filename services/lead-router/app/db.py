"""Database plumbing: connection, sessions and a timezone-safe timestamp type."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Engine, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC without a zone (works on SQLite and Postgres) and hands back aware datetimes.

    Mixing "naive" and "aware" timestamps is a classic source of off-by-hours
    bugs, so naive values are refused at the door.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("refusing to store a naive datetime; use app.db.utcnow()")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool  # one shared in-memory DB for tests
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


# Columns added after the first release. create_all() makes new tables but never changes
# existing ones, so a database from an earlier version gets these added at startup.
# Each must be nullable or have a default, so existing rows stay valid.
ADDED_COLUMNS = [
    ("leads", "max_attorneys", "INTEGER"),
    ("attorneys", "bids_link_version", "INTEGER NOT NULL DEFAULT 0"),
    ("offers", "price_note", "VARCHAR(200)"),
    ("leads", "utm_medium", "VARCHAR(120)"),
    ("leads", "utm_campaign", "VARCHAR(120)"),
    ("leads", "utm_content", "VARCHAR(120)"),
    ("leads", "referrer_host", "VARCHAR(120)"),
    ("leads", "landing_path", "VARCHAR(120)"),
    ("leads", "sensitive_consent_text", "TEXT"),
    ("leads", "gpc", "BOOLEAN"),
    ("leads", "legal_hold", "BOOLEAN"),
    ("leads", "legal_hold_reason", "VARCHAR(200)"),
    ("leads", "narrative_erased_at", "TIMESTAMP"),
    ("leads", "erased_at", "TIMESTAMP"),
    ("attorneys", "license_numbers", "JSON"),
    ("attorneys", "license_verified_at", "TIMESTAMP"),
    ("attorneys", "agreement_version", "VARCHAR(40)"),
    ("attorneys", "agreement_accepted_at", "TIMESTAMP"),
]


def upgrade_schema(engine: Engine) -> None:
    """Add any missing ADDED_COLUMNS. The router and the worker both run this at startup;
    on Postgres IF NOT EXISTS makes two of them doing it at once harmless."""
    existing = inspect(engine)
    guard = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""
    with engine.begin() as conn:
        for table, column, sql_type in ADDED_COLUMNS:
            if not existing.has_table(table):
                continue
            if column not in {c["name"] for c in existing.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {guard}{column} {sql_type}"))
