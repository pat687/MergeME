# 0003. Compare-and-set plus lead-first lock ordering for claims

**Status:** Accepted · **Date:** 2026-10-15

## Context

In the shared model several attorneys can accept the same lead within milliseconds, and
the background worker may be expiring offers at the same moment. We need:

1. exactly one winner,
2. no deadlocks between accept, decline and expiry,
3. correctness with several API replicas, without adding Redis or ZooKeeper.

## Decision

- **Claim with compare-and-set:**
  `UPDATE leads SET assigned_attorney_id=:me, status='assigned' WHERE id=:lead AND
  assigned_attorney_id IS NULL`, then check `rowcount == 1`.
- **Lock the lead row first** (`SELECT ... FOR UPDATE`) in every code path that touches a
  lead's offers, then re-read the offer. The same lock order everywhere means no cycles.
- **Outbox delivery** uses `FOR UPDATE SKIP LOCKED` so parallel workers never double-send.

## Consequences

- Correct on Postgres with any number of replicas; on SQLite (dev/tests) writes are serialized anyway.
- The losing attorney gets a clear `409 another attorney claimed this lead first`.
- Lock hold time is a few milliseconds per action. The expiry worker commits one lead at a time.
