"""The three web pages: intake form (public), offer page (attorneys), dashboard (staff)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
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


@router.get("/")
def intake_page() -> FileResponse:
    return FileResponse(STATIC / "intake.html", headers=NO_CACHE)


@router.get("/offer/{offer_id}")
def offer_page(offer_id: str) -> FileResponse:
    return FileResponse(STATIC / "offer.html", headers=NO_CACHE)


@router.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC / "admin.html", headers=NO_CACHE)
