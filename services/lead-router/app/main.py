"""Wires everything together and starts the web app.

Run locally:  uvicorn app.main:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import __version__, worker
from app.api import admin, offers, pages, public
from app.api.pages import STATIC, RevalidatedStaticFiles
from app.api.ratelimit import SlidingWindowLimiter
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory, upgrade_schema
from app.services.outbox import Handler

log = logging.getLogger("lead_router")

DESCRIPTION = """
Takes in legal inquiries, filters out junk, scores them with a transparent
rubric, and distributes them to participating attorneys using one of three
models: **exclusive** territory, **shared** (first to claim wins) or
**ping tree** (highest bidder first).

* Public: `POST /api/v1/intake`
* Attorneys: signed links to `/api/v1/offers/{id}`
* Staff: `/api/v1/admin/*` with the `X-Admin-Key` header
"""


def create_app(
    settings: Settings | None = None, handlers: dict[str, Handler] | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if settings.uses_dev_secrets:
        log.warning(
            "Using development secrets. Set ROUTER_ADMIN_API_KEY and "
            "ROUTER_LINK_SIGNING_SECRET before going live."
        )

    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    upgrade_schema(engine)
    session_factory = make_session_factory(engine)
    handlers = handlers if handlers is not None else worker.build_handlers(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = None
        if settings.embedded_worker:
            task = asyncio.create_task(worker.run_forever(session_factory, settings, handlers))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Barrgo Lead Router",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.handlers = handlers
    app.state.intake_limiter = SlidingWindowLimiter(settings.intake_rate_limit_per_minute)

    for module in (public, offers, admin, pages):
        app.include_router(module.router)
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC), name="static")
    return app
