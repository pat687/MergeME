"""The three web pages: intake form (public), offer page (attorneys), dashboard (staff)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parent.parent / "static"
# Browsers may reuse a page they already have, but must check with us first, so a new
# release never runs an old page against a new API. Unchanged files still get a 304.
NO_CACHE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


router = APIRouter(include_in_schema=False)
# The pages go through the same file server as /static, so they get ETags and answer a
# browser's "has this changed?" check with a 304 instead of the whole page.
_pages = RevalidatedStaticFiles(directory=STATIC)


@router.get("/")
async def intake_page(request: Request) -> Response:
    return await _pages.get_response("intake.html", request.scope)


@router.get("/offer/{offer_id}")
async def offer_page(offer_id: str, request: Request) -> Response:
    return await _pages.get_response("offer.html", request.scope)


@router.get("/bids/{attorney_id}")
async def bids_page(attorney_id: str, request: Request) -> Response:
    return await _pages.get_response("bids.html", request.scope)


@router.get("/admin")
async def admin_page(request: Request) -> Response:
    return await _pages.get_response("admin.html", request.scope)
