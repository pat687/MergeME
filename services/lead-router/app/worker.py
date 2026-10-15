"""The background worker: the part that keeps working when nobody is clicking anything.

Every couple of seconds it:
  1. times out offers nobody answered (and moves the ping tree to the next bidder)
  2. sends whatever is waiting in the outbox (emails, eXo updates)

Run it inside the API (simple) or as its own process: `python -m app.worker`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory, utcnow
from app.services import offers, outbox
from app.services.exo_sync import ExoLeadCaptureClient
from app.services.notifier import make_mailer

log = logging.getLogger("lead_router.worker")


def build_handlers(settings: Settings) -> dict[str, outbox.Handler]:
    handlers: dict[str, outbox.Handler] = {"email": make_mailer(settings).send}
    if settings.exo_sync_enabled:
        exo = ExoLeadCaptureClient(settings)
        handlers["exo.create_lead"] = exo.create_lead
        handlers["exo.assign_lead"] = exo.assign_lead
    return handlers


def run_cycle(
    session_factory: sessionmaker, settings: Settings, handlers: dict[str, outbox.Handler]
) -> dict[str, int]:
    now = utcnow()
    with session_factory() as db:
        expired = offers.expire_due(db, settings, now)
    delivered = outbox.deliver_due(
        session_factory, handlers, now=now, max_attempts=settings.outbox_max_attempts
    )
    if expired or delivered:
        log.info("worker cycle: expired=%s delivered=%s", expired, delivered)
    return {"expired": expired, "delivered": delivered}


async def run_forever(
    session_factory: sessionmaker, settings: Settings, handlers: dict[str, outbox.Handler]
) -> None:
    """Embedded mode: runs inside the API's event loop without blocking requests."""
    while True:
        try:
            await asyncio.to_thread(run_cycle, session_factory, settings, handlers)
        except Exception:
            log.exception("worker cycle failed; will try again")
        await asyncio.sleep(settings.worker_interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    handlers = build_handlers(settings)

    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("shutting down after the current cycle")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info(
        "worker started (interval %.1fs, eXo sync %s)",
        settings.worker_interval_seconds,
        "on" if settings.exo_sync_enabled else "off",
    )
    while not stopping:
        try:
            run_cycle(session_factory, settings, handlers)
        except Exception:
            log.exception("worker cycle failed; will try again")
        time.sleep(settings.worker_interval_seconds)


if __name__ == "__main__":
    main()
