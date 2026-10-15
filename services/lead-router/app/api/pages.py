"""The three web pages: intake form (public), offer page (attorneys), dashboard (staff)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC = Path(__file__).resolve().parent.parent / "static"

router = APIRouter(include_in_schema=False)


@router.get("/")
def intake_page() -> FileResponse:
    return FileResponse(STATIC / "intake.html")


@router.get("/offer/{offer_id}")
def offer_page(offer_id: str) -> FileResponse:
    return FileResponse(STATIC / "offer.html")


@router.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC / "admin.html")
