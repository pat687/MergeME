# 0002. Build routing as a separate service, not an eXo add-on

**Status:** Accepted · **Date:** 2026-10-15

## Context

eXo's `lead-capture` add-on stores leads with a status pipeline and an assignee, but it was
built for eXo's own software sales. It has no practice areas, ZIP territories, bidding,
time-limited offers or billing. We could extend it in Java inside eXo, or build a separate
service.

## Decision

Build a separate **Lead Router** in Python (FastAPI + SQLAlchemy 2 + Postgres). It is the
system of record for leads. eXo optionally receives a mirror of each lead through the
add-on's REST API, delivered via the outbox.

## Why

- **Different rates of change.** Routing rules change often; eXo upgrades are big and rare.
- **Failure isolation.** An eXo restart or upgrade must not stop intake.
- **Testability.** The suite runs 154 tests in a few seconds (plus 5 Postgres concurrency tests). Testing
  inside an eXo container takes minutes to boot.
- **Version coupling.** Add-ons must be compiled against the exact eXo version (see
  `infra/exo/README.md`). A REST integration avoids that coupling.

## Consequences

- Two UIs: staff use the router dashboard for leads and eXo for everything else. SSO between
  them is on the roadmap.
- The REST contract with lead-capture must be watched across eXo upgrades. It's covered by
  contract tests with a fake eXo (`tests/test_outbox_and_exo.py`).
