"""The outbox: a to-do list of messages, saved in the database, sent by a worker.

Why not just send the email right away? If the email server is down, or the
request crashes halfway, we'd either lose the message or send one about a
change that never got saved. Writing the message into the same database
transaction as the change, then sending it afterwards with retries, avoids
both problems. This is the "transactional outbox" pattern.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import utcnow
from app.models import OutboxMessage

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], None]


def enqueue(db: Session, kind: str, payload: dict[str, Any]) -> None:
    db.add(OutboxMessage(kind=kind, payload=payload, next_attempt_at=utcnow()))


def backoff(attempts: int) -> timedelta:
    """10s, 20s, 40s, 80s... capped at 5 minutes."""
    return timedelta(seconds=min(300, 5 * 2**attempts))


def deliver_due(
    session_factory: sessionmaker,
    handlers: dict[str, Handler],
    *,
    now: datetime,
    max_attempts: int,
    batch_size: int = 50,
) -> int:
    """Send everything that's due. Returns how many messages were delivered."""
    delivered = 0
    with session_factory() as db:
        due = db.scalars(
            select(OutboxMessage)
            .where(
                OutboxMessage.sent_at.is_(None),
                OutboxMessage.failed.is_(False),
                OutboxMessage.next_attempt_at <= now,
            )
            .order_by(OutboxMessage.id)
            .limit(batch_size)
            # On Postgres, two workers never grab the same message.
            .with_for_update(skip_locked=True)
        ).all()

        for msg in due:
            try:
                handler = handlers.get(msg.kind)
                if handler is None:
                    raise LookupError(f"no handler registered for {msg.kind!r}")
                handler(msg.payload)
                msg.sent_at = now
                delivered += 1
            except Exception as exc:
                msg.attempts += 1
                msg.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                if msg.attempts >= max_attempts:
                    msg.failed = True
                    log.error("outbox message %s gave up after %s tries", msg.id, msg.attempts)
                else:
                    msg.next_attempt_at = now + backoff(msg.attempts)
                    log.warning("outbox message %s failed, will retry: %s", msg.id, exc)
        db.commit()
    return delivered
