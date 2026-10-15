# 0005. Transactional outbox for email and eXo sync

**Status:** Accepted · **Date:** 2026-10-15

## Context

Routing a lead triggers side effects: offer emails, a consumer receipt, a delivery email
and an eXo sync. Sending them inline inside the request causes two failure modes:

- the email is sent, then the transaction rolls back, so someone hears about a lead that
  doesn't exist;
- the transaction commits, then the SMTP call fails, so the email is lost.

## Decision

Write each side effect as an `outbox_messages` row **in the same transaction** as the state
change. A worker (`app/worker.py`) delivers due messages every 2 seconds:

- success → `sent_at` set;
- failure → `attempts+1`, `next_attempt_at = now + min(5 min, 5s × 2^attempts)`;
- after `ROUTER_OUTBOX_MAX_ATTEMPTS` → `failed=true`, counted on the dashboard;
- unknown message kinds fail loudly instead of being dropped.

## Consequences

- At-least-once delivery. A message could be sent twice if the worker crashes between
  sending and committing. That's acceptable for notifications. eXo deduplicates leads by
  email address.
- Emails arrive up to one worker interval later (about 2 seconds).
- The API can run without the worker for development (`ROUTER_EMBEDDED_WORKER=true`, the default).
