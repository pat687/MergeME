# 0004. A ledger that can only express flat fees

**Status:** Accepted · **Date:** 2026-10-15

## Context

Attorney ethics rules (ABA Model Rules 5.4(a), 1.5(e), 7.2) forbid sharing legal fees with
non-lawyers and paying for recommendations, while allowing payment for advertising and
lead generation. A pricing feature like "15% of the settlement" would be a serious
violation, and such features tend to arrive as "just one more field".

## Decision

Make the violation **unrepresentable** rather than merely against policy:

- `ChargeKind` has only `per_lead`, `included`, `subscription` and `territory`.
- Every ledger write passes through `compliance.assert_flat_fee` (whole cents, non-negative,
  known kind).
- Attorney API schemas use `extra="forbid"`, so a `fee_percent` field is rejected.
- Charges happen at delivery. There is no endpoint to report retention or case outcome.
- Tests assert all of the above, including that no charge kind name mentions settlement,
  retention, contingency, percent, share or recovery.
- A unique key `(attorney, kind, period, reference)` makes billing idempotent.

## Consequences

- Revenue models are limited to pay-per-lead, subscriptions and territory rentals. That's
  intended.
- Adding a new pricing model requires changing the enum, the guard and the tests, which is a
  visible, reviewable change.
