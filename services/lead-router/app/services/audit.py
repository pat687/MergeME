"""The lead's diary. Every decision about a lead gets one line, in order, forever."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import AuditEvent


def record(db: Session, lead_id: str, event: str, **detail: Any) -> None:
    db.add(AuditEvent(lead_id=lead_id, at=utcnow(), event=event, detail=detail))
