# 0001. Use eXo Platform Community instead of SharePoint

**Status:** Accepted · **Date:** 2026-10-15

## Context

The team uses SharePoint for documents, team sites and lists. The goals are to stop paying
per-seat licences, keep privileged legal material on our own hardware, and avoid lock-in,
while keeping document co-editing and team spaces.

Options considered:

| Option | Strengths | Weaknesses |
|---|---|---|
| Stay on SharePoint | Familiar; Office integration | Licence cost, data residency, lock-in |
| Nextcloud | Excellent file sync | Weaker social/space/intranet model |
| HumHub | Lightweight social intranet | Thin document management |
| Liferay | Very powerful portal | Heavy; steep learning curve |
| **eXo Platform Community** | Spaces, documents, wiki (Notes), tasks, activity streams; official Docker images; ONLYOFFICE integration | Heavier footprint (Elasticsearch, MySQL); smaller community |

## Decision

Run **eXo Platform Community 7.2** from the official image, with MySQL, Elasticsearch and
ONLYOFFICE, behind Nginx. The layout follows eXo's official compose file, adapted with
health-gated startup and generated secrets.

## Consequences

- No per-seat licence. Data stays on our infrastructure.
- We operate more moving parts. That's mitigated by compose health checks, a runbook and
  backup commands.
- Migration effort for existing SharePoint content; see `docs/SHAREPOINT_TO_EXO.md`.
- First boot needs about 10 GB of RAM across services, which is fine on a single server and
  heavy on small laptops (hence the separate `make up` and `make up-full`).
