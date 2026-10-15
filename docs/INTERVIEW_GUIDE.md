# Interview guide

How to present this project in an IT interview: a pitch, a live demo script, the design
decisions worth highlighting, and answers to the questions you're most likely to get.

---

## 1. The 30-second pitch

> "I replaced a SharePoint dependency with a self-hosted, open-source stack and built a
> lead-distribution service on top of it. The service takes legal inquiries from the web,
> filters out spam, scores each lead with a transparent rubric, and distributes it to
> attorneys using one of three models: exclusive territories, shared first-come, or a
> ping-tree auction. It's built to follow attorney-advertising rules: it can only charge flat
> fees, so fee-splitting is impossible by construction. It's race-safe, every side effect
> goes through a transactional outbox, every decision is explainable, and the whole thing
> starts with one command and is covered by 249 automated tests, including real
> concurrency tests on Postgres, plus CI."

## 2. The 5-minute live demo

| # | Do this | Say this |
|---|---|---|
| 1 | `make up` | "One command: it generates random secrets, builds a non-root image, waits on health checks." |
| 2 | Open http://localhost:8000 | "Consumer side. Note the disclosure: advertising, not a law firm, attorneys pay. That wording is a compliance requirement." |
| 3 | `make demo` | "This plays out five inquiries, one per outcome: auction, exclusive, shared, spam, and no coverage." |
| 4 | Point at the score breakdown | "Every point has a reason. If ops asks why a lead was a B, we can show them." |
| 5 | Point at the shared race (200 vs 409) | "Riley got there first, so Morgan gets a 409. The truly simultaneous case, threads racing on real Postgres, is proven in `test_concurrency_postgres.py`." |
| 6 | Open http://localhost:8000/admin and click a lead | "The decision trail: who was skipped and why, every offer, every timeout." |
| 7 | Click **open as attorney** on an offer | "Attorneys get a signed link. Until they accept, contact details are withheld and the description is redacted." |
| 8 | Open http://localhost:8025 | "Every email went through the outbox. If SMTP were down, they'd retry with backoff." |
| 9 | Open http://localhost:8000/docs | "The OpenAPI docs are generated from the same Pydantic models that validate input." |
| 10 | (If time) `make up-full`, http://exoapp.local | "The SharePoint replacement: spaces, documents, wiki, in-browser Office editing." |

## 3. Design decisions to highlight

1. **Pure domain core.** Scrubbing, scoring, matching and distribution are pure functions in
   `app/domain/`. No database, no clock. They read like the business spec and test in milliseconds.
2. **Strategy-style distribution.** The planner returns a *plan* (offers to create, attorneys
   skipped with reasons). Applying the plan is a separate step, so adding a fourth model
   (say, round-robin) touches one function.
3. **Compare-and-set for "first click wins".** `UPDATE leads SET assigned=:me WHERE
   assigned IS NULL`, checking `rowcount`. It works across many API replicas without a
   distributed lock service.
4. **Consistent lock ordering.** Accept, decline and the expiry worker all lock the lead row
   before its offers. That prevents deadlocks.
5. **Transactional outbox.** Side effects commit atomically with state changes and are
   delivered at least once, with exponential backoff and a dead-letter state.
6. **Compliance by construction.** No percentage charge type exists. The API forbids unknown
   fields. A test fails if consumer wording ever claims "best" or "evaluated your case".
7. **Signed links instead of attorney passwords.** HMAC tokens are stateless and unforgeable,
   and a bad token returns the same 404 as a missing offer, which prevents enumeration.
8. **Explainability as a feature.** An append-only audit trail per lead, shown in the UI.
9. **Ops-friendly.** Health and readiness endpoints, idempotent billing, generated secrets,
   one image for API and worker, and `make` targets for everything.
10. **Buy vs build.** eXo for collaboration (buy/adopt), custom code only where the business
    is unique (routing). The eXo integration is optional and failure-isolated.

## 4. Likely questions, with answers

**Why not just use SharePoint?**
Licensing cost per user, data residency (privileged legal data stays on our hardware), and
vendor lock-in. eXo gives spaces, documents, wiki, tasks and, with ONLYOFFICE, in-browser
editing of Office files, all open source and self-hosted.

**Why a separate service instead of an eXo add-on?**
Different change rates and risk profiles. Routing logic changes weekly, while eXo upgrades
are quarterly and heavy. A separate service can be deployed, scaled and tested on its own,
and an eXo outage can't stop leads from flowing. eXo still gets the data through the
lead-capture add-on's REST API.

**What happens if two attorneys accept at the same millisecond?**
Both requests try `SELECT ... FOR UPDATE` on the lead row, so one waits. The first locks the
attorney row, re-checks their daily cap, runs the conditional update (1 row), marks the
other offers lost, and commits. The second then sees its offer is `lost` and returns 409
"another attorney claimed this lead first". Every write is a compare-and-set, so even
without row locks (SQLite) a stale writer changes nothing. `test_concurrency_postgres.py`
fires real threads through a barrier at Postgres to prove it, and I checked that each test
fails when its fix is removed.

**How do you prevent losing emails?**
The outbox row is written in the same transaction as the change. If the transaction commits,
the email will be sent eventually; if it rolls back, it never existed. The worker retries
with backoff (10s, 20s, 40s... capped at 5 minutes) and parks a message after N failures so
ops can see it.

**How would you scale it?**
The API is stateless, so add replicas. Workers use `SKIP LOCKED`, so add workers. At
thousands of attorneys, move candidate filtering into SQL with indexes on state, practice
area and ZIP3. For sub-second ping trees, replace polling with a delayed queue.

**How do you test time-based behavior like offer expiry?**
Time is a parameter everywhere (`now`). Tests call `expire_due(db, settings, now + 91s)`.
No sleeping and no clock mocking.

**How is the scoring fair or explainable?**
It's a rubric, not a black box: each rule returns points and a sentence. The total is
clamped to 0–100, and the breakdown is stored on the lead and shown in the dashboard.
Scores never auto-reject a lead. Flags like "time-sensitive" are notes for the attorney.

**What are the security considerations?**
Admin key compared in constant time (as bytes, so odd input can't cause a 500);
HMAC-signed attorney links with the token after `#` so it never reaches server logs;
contact fields withheld and descriptions redacted before acceptance; forged
`X-Forwarded-For` ignored, so the consent IP is trustworthy; per-IP rate limit on intake;
XSS-safe DOM rendering; non-root containers; secrets generated per install and never
committed; naive datetimes refused at the DB boundary.

**What's the legal risk and how does the code address it?**
The main risks are fee-splitting, implying a recommendation, and consent for calls. The
ledger can only record flat fees, the wording is centralized and tested, and consent text,
version, time, IP and user agent are stored on every lead. See `docs/COMPLIANCE.md`. I'd
still have counsel review before launch, because state rules differ.

**What would you do next?**
Alembic migrations, a shared rate limit at the proxy (today's is per process), Prometheus
metrics, an attorney self-service portal (bids, caps, pause), SSO between eXo and the
dashboard, expiring offer links, and a data-retention job.

**What was the hardest bug?**
On first boot, eXo's document service failed with bean-creation errors. The root cause was
that eXo gave up waiting for Elasticsearch after 20 seconds. Fix: a real Elasticsearch
health check, and `depends_on: condition: service_healthy`. After that, Elasticsearch went
red because the disk was over its 90% watermark, so it refused to place shards. Documented
in the runbook, with a local-only override.

**How did you review your own work?**
I ran an adversarial multi-agent review on my own pull request. Six reviewers each covered
one area (concurrency, security, business rules, infrastructure, docs, frontend), and three
skeptics tried to disprove every finding. 40 survived. Examples: an attorney could accept
past their daily cap, a description could leak contact details into previews, the ping
tree could exceed the number of attorneys named in the consent, and "Pass" declined even
when you hit Cancel. Each got a fix and a regression test. Finding your own bugs before a
reviewer does is the point.

## 5. Explaining it to a non-technical interviewer

Use the analogies in [ELI10.md](ELI10.md):
- **eXo** = our own clubhouse with a filing cabinet, instead of renting Microsoft's.
- **Router** = a very organized mail room: junk filter → grade with reasons → hand out fairly.
- **Exclusive / shared / ping tree** = "I rent this street" / "first hand up wins" / "auction line".
- **Flat fees** = lawyers buy a ticket per lead and never pay a cut of their winnings.

## 6. Numbers to remember

| Metric | Value |
|---|---|
| Automated tests | 249: unit, end-to-end API stories, and 5 Postgres concurrency tests, plus 26 browser checks |
| Distribution models | 3 |
| Docker services | 9 (4 for the router, 5 for eXo) |
| Router image | Multi-stage, non-root, ~300 MB |
| eXo first boot | About 3.5 minutes on 4 cores (fresh database), about 2 minutes after that |
| Business rules (`app/domain/`) | About 540 lines, all pure functions |
