# Runbook

How to run, check, back up and fix the stack. Commands are run from the repo root.

## Daily operations

| Task | Command |
|---|---|
| Start router only | `make up` |
| Start everything (with eXo) | `make up-full` |
| Stop (keeps data) | `make down` |
| Follow router logs | `make logs` |
| What's running? | `docker compose --profile exo ps` |
| Run worker cycle now | Dashboard → **Run worker now**, or `POST /api/v1/admin/worker/run` |
| Monthly billing | Dashboard → **Run monthly billing** (safe to repeat) |
| Back up router DB | `make backup` → `backups/router-YYYYMMDD-HHMMSS.sql.gz` |

## Health checks

| Check | Healthy answer |
|---|---|
| `curl localhost:8000/healthz` | `{"status":"ok"}`: the process is alive |
| `curl localhost:8000/readyz` | `{"status":"ready"}`: it can reach Postgres |
| Dashboard "Email queue" | `0 failed`; pending should drain within seconds |
| `curl -s -o /dev/null -w '%{http_code}' http://exoapp.local/portal/login` | `200` |
| `docker compose exec exo-search curl -s localhost:9200/_cluster/health` | `"status":"green"` or `"yellow"` |

## First boot of eXo

- On a fresh database it takes about 3–4 minutes on a 4-core machine (measured: all services
  healthy after 201 s; Tomcat reported `Server startup in [167646] milliseconds`). Later
  restarts take about 2 minutes. The `exo` container shows `health: starting` until
  `/portal/login` answers.
- Open http://exoapp.local. The **Account Setup** screen asks for your own account and a
  password for the built-in `root` super-administrator. Submit, then click **Start**.
- Then create spaces (see [SHAREPOINT_TO_EXO.md](SHAREPOINT_TO_EXO.md#2-suggested-space-layout)).
- Expected first-boot log noise from eXo 7.2.0 itself: many `Duplicate entry ... SOC_METADATAS`
  errors from its parallel initial import, MySQL `utf8mb3` deprecation warnings, and
  `Could not initialize Matrix service` (chat needs a separate Matrix server). In testing,
  none of these stopped the portal from starting.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make up` says `run make init` | No `.env` | `make init` |
| Port 8000 (or 8025, or 80) is already in use | Another program has it | Set `ROUTER_HTTP_PORT`, `MAILPIT_UI_PORT` or `EXO_HTTP_PORT` in `.env`. The links in emails follow `ROUTER_HTTP_PORT` automatically, and eXo's links, proxy port and ONLYOFFICE host follow `EXO_HTTP_PORT` (the proxy listens on that port inside Docker too, so eXo and ONLYOFFICE can reach each other; verified on 8088). |
| Intake returns `429 Too Many Requests` | Per-IP rate limit (default 10 per minute) | Expected under abuse. For load tests, raise `ROUTER_INTAKE_RATE_LIMIT_PER_MINUTE` (or set it to 0). |
| Behind a reverse proxy, many visitors get `429`, and leads show the same IP (e.g. `172.19.0.1`) | The router doesn't trust the proxy, so every request looks like it comes from one address. Through a published Docker port the router sees the Docker network's gateway, not `127.0.0.1` | Set `ROUTER_TRUSTED_PROXY_IPS` to the address the router sees (see [Behind a reverse proxy](#behind-a-reverse-proxy)), then run `make up`. `docker compose restart` keeps the old value |
| An attorney gets "you can't take this lead right now: daily lead cap reached" | They've accepted `daily_cap` leads since local midnight (`ROUTER_BUSINESS_TIMEZONE`) | Raise their cap with `PATCH /api/v1/admin/attorneys/{id}`, or wait until midnight |
| An intake is rejected with "consent was given on an outdated form" | `ROUTER_DISCLOSURE_VERSION` changed while their page was open | Expected: they need to reload and agree to the current wording |
| Image pull fails with `429 Too Many Requests` | Docker Hub anonymous rate limit | `docker login`, wait, or use a mirror (e.g. `mirror.gcr.io/library/postgres:16.15`, then `docker tag` it to the expected name) |
| Leads stay `offered` past the deadline | Worker not running | `docker compose ps lead-worker`; `docker compose up -d lead-worker`. Opening the offer link also expires it lazily. |
| Emails not arriving | SMTP unreachable | Dashboard shows pending/failed counts; `docker compose logs lead-worker`. Messages retry automatically. |
| Dashboard says "admin key required" | Wrong key | It's `ROUTER_ADMIN_API_KEY` in `.env` |
| Attorney link says "offer not found" | Link was altered or the signing secret changed | Links are signed with `ROUTER_LINK_SIGNING_SECRET`; changing it invalidates outstanding links |
| eXo log: `20,000 milliseconds timeout` then `webDavPathMappingDao` bean errors | eXo started before Elasticsearch was ready | Compose now waits for `exo-search` to be healthy. If you changed that, restore it and restart `exo`. |
| Elasticsearch cluster `red`, eXo search/documents broken | Disk over 90% full: ES refuses to place shards (high watermark) | Free disk space. The local stack disables the watermark (`cluster.routing.allocation.disk.threshold_enabled=false`). Production should keep it and monitor disk instead. |
| `make up-full` on a Mac fails with `no matching manifest for linux/arm64` | eXo publishes Intel-only images; an older copy of `docker-compose.yml` doesn't ask for the Intel one | Update to a compose file with `platform: linux/amd64` on the `exo` service, and turn on Rosetta emulation in Docker Desktop (Settings → General). See the README's Mac section. |
| `exoapp.local` doesn't resolve | No hosts entry | Add `127.0.0.1 exoapp.local` to `/etc/hosts` (Windows: `C:\Windows\System32\drivers\etc\hosts`) |
| eXo container killed / restarting | Not enough memory | eXo needs ~4 GB, Elasticsearch ~1.5 GB, ONLYOFFICE ~1.5 GB, MySQL ~0.5 GB. Lower `EXO_JVM_SIZE_MAX` or add RAM. |

## Behind a reverse proxy

The router stores each person's IP address with their consent and limits intake
submissions per IP. Both rely on knowing the real visitor address, which a reverse proxy
(nginx, Caddy, a load balancer) passes in `X-Forwarded-For`. The router only believes that
header from addresses listed in `ROUTER_TRUSTED_PROXY_IPS` (addresses or CIDR ranges), so a
visitor can't pick their own IP.

Which address to list is the address the router *sees* the proxy as:

| Where the proxy runs | What the router sees | Set `ROUTER_TRUSTED_PROXY_IPS` to |
|---|---|---|
| On the host, connecting to the published port | The Docker network's gateway, e.g. `172.19.0.1` (not `127.0.0.1`) | That gateway: `docker network inspect barrgo_default -f '{{(index .IPAM.Config 0).Gateway}}'` |
| In a container on the same Compose network | That container's address, which can change on restart | The network's subnet, e.g. `172.19.0.0/16` (`... -f '{{(index .IPAM.Config 0).Subnet}}'`) |

After changing it in `.env`, run `make up` so the router container is recreated with the new
value (`docker compose restart` keeps the old one).

Until this is set, every visitor looks like the proxy: the per-IP limit becomes one limit
for the whole site and the stored consent IP is the proxy's. When a proxy is in front, also
publish the router only on loopback (`127.0.0.1:8000:8000`) so nothing can bypass the proxy.

**Pin the network first.** Docker picks the network's address range when it creates it, and
`make down` removes it, so the next `make up` can land on a different gateway and the trusted
address silently stops matching. A `docker-compose.override.yml` next to `docker-compose.yml`
(Compose reads it automatically) fixes the range to one outside Docker's own pools:

```yaml
networks:
  default:
    ipam:
      config:
        - subnet: 10.203.57.0/24
```

Run `make down && make up` once to recreate the network. From then on the gateway is always
`10.203.57.1` (a proxy on the host) and the subnet `10.203.57.0/24` (a proxy container).

## Backups and restore

Router (leads, offers, ledger, audit):

```bash
make backup        # prints "Backup FAILED" and exits non-zero if pg_dump didn't finish

# Restore, but only from a file that is intact (gunzip -t) and complete (pg_dump writes
# "PostgreSQL database dump complete" as its last line).
f=backups/router-20261015-020000.sql.gz
if gunzip -t "$f" && gunzip -c "$f" | tail -n 5 | grep -q 'PostgreSQL database dump complete'; then
  docker compose stop lead-router lead-worker     # nothing writes while we restore
  gunzip -c "$f" | docker compose exec -T router-db psql -1 -v ON_ERROR_STOP=1 -U router router
  docker compose start lead-router lead-worker
else
  echo "NOT RESTORING: $f is damaged or incomplete" >&2
fi
```

The dump includes `DROP ... IF EXISTS` statements, so restoring replaces the current data.
`-1` runs the restore as one transaction and `ON_ERROR_STOP=1` stops at the first error, so
a dump that fails part-way rolls back instead of leaving half the tables restored.
The checks before it matter because some bad files contain no error at all: a dump cut off
at a statement boundary, or a `.gz` that ends early at a line break, would otherwise restore
"successfully" with tables missing. Both are refused before anything is dropped.

eXo keeps data in three places: MySQL (`exo_db_data`), files (`exo_data`) and the search
index (`exo_search_data`, which can be rebuilt). Back up the first two together while eXo is stopped:

```bash
docker compose stop exo
docker compose exec -T exo-db sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" exo' | gzip > backups/exo-db-$(date +%F).sql.gz
docker run --rm -v barrgo_exo_data:/data -v "$PWD/backups":/b alpine tar czf /b/exo-files-$(date +%F).tgz -C /data .
docker compose start exo
```

## Upgrades

1. Read the eXo release notes; back up first.
2. Change the image tag in `docker-compose.yml` (or `EXO_IMAGE` in `.env`).
3. `docker compose --profile exo pull && docker compose --profile exo up -d`.
4. Router: `git pull && make up`. New tables are created automatically, and the few columns
   added since the first release are added at startup (`ADDED_COLUMNS` in `app/db.py`). Add
   Alembic before the first production schema change that isn't a new nullable column (see
   ARCHITECTURE.md §11).
5. After upgrading to the release with attorney bid pages: every practice area now has a
   lowest price (`ROUTER_FLOOR_PRICE_CENTS`, $15 by default outside the auctioned areas),
   including set-price areas. Attorneys paying per lead whose price is below it stop getting
   leads in that area. Open the dashboard's Market panel: anyone affected is marked
   "below lowest bid", and you can re-price them with `PATCH /api/v1/admin/attorneys/{id}`.
6. After upgrading to the release with the legal guardrails, **no inquiry is accepted until
   you list states** in `ROUTER_LAUNCH_STATES`. To keep a local demo working, copy the
   "Where leads may be sold" block from `.env.example` into your `.env` (demo values for
   the fictional Illinois network) and run `make up`. For real use, set them from counsel's
   written approval; see [COMPLIANCE.md](COMPLIANCE.md#2-where-leads-may-be-sold). Also record
   a bar-license check for every attorney (`POST /api/v1/admin/attorneys/{id}/license-check`)
   or they stop getting leads; the demo `.env` turns that check off.

## People's data: requests, holds and retention

The full procedure is in the privacy and security program (legal review pack, kept outside
this public repository).
The tools (all need the admin key; try them at `/docs`):

| Task | Call |
|---|---|
| Find everything about a person (access request) | `GET /api/v1/admin/privacy/lookup?email=...` or `?phone=...` |
| Delete their story and health details, keep consent evidence | `POST /api/v1/admin/leads/{id}/erase` with `{"level": "narrative", "reason": "request #..."}` |
| Delete everything, including name and contact details | the same with `"level": "full"` |
| Stop all deleting for a lead (lawsuit, subpoena, complaint) | `POST /api/v1/admin/leads/{id}/legal-hold` with `{"on": true, "reason": "..."}` |
| Run the retention schedule now | `POST /api/v1/admin/retention/run` (the worker also runs it every 10 minutes) |

Erasing a lead still on offer withdraws it, and attorneys who received the contact details
are emailed a request to delete them. Only that lead's emails are blanked, never another
inquiry's from the same person, and a legal hold also keeps the lead's sent emails. Erasing can't reach copies outside the router: backups
(they age out), attorneys' own mailboxes, and eXo if `ROUTER_EXO_SYNC_CONTACT_DETAILS=true`.

## Attorneys: license checks and the agreement

- Before an attorney gets leads, look them up on each state bar's website and record it:
  `POST /api/v1/admin/attorneys/{id}/license-check` with `{"license_numbers": {"IL": "6301234"}}`.
  Repeat at least every `ROUTER_LICENSE_RECHECK_DAYS` (90); after that they stop getting leads.
- Once counsel has a participation agreement, set `ROUTER_ATTORNEY_AGREEMENT_VERSION` and
  record each acceptance: `POST /api/v1/admin/attorneys/{id}/agreement` with `{"version": "..."}`.
  Attorneys who haven't accepted that version stop getting leads.

## Going to production

With `ROUTER_DEMO_MODE=false` the router refuses to start unless the public URL is
`https://`, the mail server isn't Mailpit, license checks are on, and the legal entity name
and postal address are set.

- [ ] Real domain + TLS (terminate at `exo-proxy` or an upstream load balancer)
- [ ] `ROUTER_DEMO_MODE=false`, and secrets from a secret manager rather than `.env`
- [ ] `ROUTER_LAUNCH_STATES` and `ROUTER_COUNSEL_CLEARED` from counsel's written approval;
  `ROUTER_LEGAL_ENTITY_NAME`, `ROUTER_POSTAL_ADDRESS`, `ROUTER_PRIVACY_EMAIL`
- [ ] Single sign-on with MFA in front of `/admin` and `/docs` at the proxy
- [ ] Encrypted disks, and encrypted backups kept off the machine for 30 days
- [ ] Real SMTP provider (`ROUTER_SMTP_HOST`, `_USERNAME`, `_PASSWORD`, `_STARTTLS=true`)
- [ ] Remove Mailpit and the Elasticsearch disk-watermark override
- [ ] Nightly backups copied off the machine, plus a tested restore
- [ ] Monitoring on `/readyz`, outbox failed count, disk space and container restarts
- [ ] A shared rate limit on `/api/v1/intake` at the proxy (the built-in one is per process)
- [ ] If a proxy sits in front of the router, set `ROUTER_TRUSTED_PROXY_IPS` to the address the
  router sees it as (see [Behind a reverse proxy](#behind-a-reverse-proxy))
- [ ] Legal sign-off: see [COMPLIANCE.md](COMPLIANCE.md#7-launch-checklist-business-and-legal)
