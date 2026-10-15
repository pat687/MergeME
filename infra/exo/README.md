# eXo Platform + the lead-capture add-on (optional)

The stack runs eXo Platform Community **as-is** from the official image. You only need
this folder if you want each routed lead mirrored into eXo's **lead-capture** add-on
(`ROUTER_EXO_SYNC_ENABLED=true`), so staff can see the pipeline inside eXo next to their
documents and tasks.

The router never depends on this. With sync off, or with eXo down, leads are routed,
billed and emailed exactly the same.

## What the add-on is

[exo-addons/lead-capture](https://github.com/exo-addons/lead-capture) is eXo's own add-on
for capturing and managing leads: forms, campaigns, a status pipeline
(open → qualified → accepted → recycled), an assignee per lead, tasks and mail templates.
eXo built it for its own sales team, so it has no concept of practice areas, ZIP territories
or bidding. That's why routing lives in the Lead Router, and eXo only receives the result.

## Version matching (read this first)

An eXo add-on is compiled against a specific eXo/Meeds version. As of October 2026 the
add-on has no release that targets eXo **7.2**:

| lead-capture ref | Built against (from its `pom.xml`) | Use with |
|---|---|---|
| `1.0.0-M42` | Meeds tasks `7.0.0-exo-RC03` | eXo 7.0.x |
| `1.0.0-M43` | Meeds tasks `7.3.0-M09` | eXo 7.3 milestones |
| `develop` | `7.3.x-SNAPSHOT` | eXo 7.3 development builds |

Pick the pair that matches. For example, run the whole stack on 7.0 with the M42 add-on:

```bash
make exo-addon EXO_BASE_IMAGE=exoplatform/exo-community:7.0 LEADCAPTURE_REF=1.0.0-M42
# then in .env:
EXO_IMAGE=barrgo/exo-leadcapture:local
```

Check that the base image tag exists on Docker Hub (`exoplatform/exo-community`) before building.

> **Verification status:** the Dockerfile follows the add-on's own packaging (a zip with
> `webapps/*.war` and `lib/*.jar`), but it was not built end to end while this repo was
> written, because the build sandbox couldn't reach `repository.exoplatform.org`. Treat it
> as a starting point and run it once on a machine with normal internet access.

## Turning sync on

1. Build and run eXo with the add-on (above), then `make up-full`.
2. In eXo, open the lead-capture settings, enable capture and set a **capture token**.
3. Create an eXo user for the router and add it to the `ux-team` group (the add-on's
   REST endpoints for listing and assigning leads require that group).
4. Give each attorney an eXo account and set their `exo_username` in the router
   (`PATCH /api/v1/admin/attorneys/{id}`).
5. In `.env`:

   ```bash
   ROUTER_EXO_SYNC_ENABLED=true
   ROUTER_EXO_LEADCAPTURE_TOKEN=<capture token>
   ROUTER_EXO_USERNAME=<router user>
   ROUTER_EXO_PASSWORD=<router user password>
   ```

6. `docker compose up -d lead-router lead-worker` to apply.

## What gets sent

| Router event | eXo REST call |
|---|---|
| Lead routed (offered or exclusive) | `POST /portal/rest/leadcapture/leadsmanagement/leads` with header `token`, body `{lead: {...}, response: {formName, fields[]}}` |
| Lead assigned | `GET /portal/rest/leadcapture/leadsmanagement/leads?search=<email>`, then `PATCH /portal/rest/leadcapture/leadsmanagement/assign` with `{id, assignee}` |

Payload builders and the HTTP client: [`app/services/exo_sync.py`](../../services/lead-router/app/services/exo_sync.py).
Contract tests (with a fake eXo): [`tests/test_outbox_and_exo.py`](../../services/lead-router/tests/test_outbox_and_exo.py).
