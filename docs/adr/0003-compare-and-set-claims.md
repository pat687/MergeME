# 0003. Compare-and-set plus lead-first lock ordering for claims

**Status:** Accepted · **Date:** 2026-10-15 (revised after the adversarial review)

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
- **Every other transition is a compare-and-set too** (offer `pending → x` only `WHERE
  status='pending'`, lead → `unsold` only while still offered and unowned), so a writer with
  stale data changes nothing. That also keeps SQLite, which has no row locks, correct.
- **Lock order: lead row, then attorney row** (`SELECT ... FOR UPDATE`) in every code path.
  The attorney lock makes the daily-cap check plus the claim atomic. The same order
  everywhere means no cycles.
- **Advisory locks for duplicate intakes:** `pg_advisory_xact_lock` on hashes of
  (email, practice area) and (phone, practice area), taken in sorted order.
- **Outbox delivery** uses `FOR UPDATE SKIP LOCKED` so parallel workers never double-send.
- **Proof:** `tests/test_concurrency_postgres.py` runs real threads through a barrier
  against Postgres in CI. Each test was checked to fail with its fix removed.

## Consequences

- Correct on Postgres with any number of replicas; on SQLite (dev/tests) writes are serialized anyway.
- The losing attorney gets a clear `409 another attorney claimed this lead first`.
- Lock hold time is a few milliseconds per action. The expiry worker commits one lead at a time.
