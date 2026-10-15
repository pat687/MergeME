#!/usr/bin/env python3
"""A narrated, end-to-end demo against a running stack. Standard library only.

    make up && make demo

It plays out five inquiries and shows each distribution model, the score math,
the "first click wins" race, billing and the dashboard numbers.
Requires ROUTER_DEMO_MODE=true (the default in .env.example) so the admin API
returns the attorneys' signed offer links.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = os.environ.get("ROUTER_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
INBOX = f"http://localhost:{os.environ.get('MAILPIT_UI_PORT', '8025')}"
KEY = os.environ.get("ROUTER_ADMIN_API_KEY", "dev-admin-key-change-me")
RUN = f"{int(time.time()) % 100000:05d}"  # keeps emails unique so re-runs aren't duplicates

BOLD, DIM, GREEN, YELLOW, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = YELLOW = RED = RESET = ""


def call(method: str, path: str, body: dict | None = None, admin: bool = False) -> tuple[int, dict]:
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if admin:
        req.add_header("X-Admin-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    """The demo doubles as a smoke test: CI fails if any expectation is wrong."""
    if not ok:
        FAILURES.append(what)
        print(f"  {RED}x expected: {what}{RESET}")


def step(n: int, title: str, why: str) -> None:
    print(f"\n{BOLD}Step {n}. {title}{RESET}\n{DIM}{why}{RESET}")


def person(first: str, last: str, **overrides) -> dict:
    data = {
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}.{last.lower()}.{RUN}@example.org",
        "phone": f"312{random.randint(200, 999)}{random.randint(1000, 9999)}",
        "zip_code": "60601",
        "state": "IL",
        "practice_area": "personal_injury",
        "description": (
            "I was rear-ended at a red light on Michigan Avenue. My neck and back hurt and I "
            "went to urgent care the next morning. The other driver's insurer keeps calling me."
        ),
        "incident_date": (date.today() - timedelta(days=6)).isoformat(),
        "injured": True,
        "medical_treatment": True,
        "consent": True,
        "consent_version": "2026-10-01",
    }
    data.update(overrides)
    return data


def submit(data: dict) -> dict:
    status, body = call("POST", "/api/v1/intake", data)
    assert status == 201, body
    colour = {"assigned": GREEN, "offered": GREEN, "rejected": RED}.get(body["status"], YELLOW)
    print(f"  {data['first_name']:<8} {data['practice_area']:<16} {data['zip_code']}  ->  "
          f"{colour}{body['status']}{RESET}")
    return body


def lead(lead_id: str) -> dict:
    return call("GET", f"/api/v1/admin/leads/{lead_id}", admin=True)[1]


def attorneys() -> dict[int, str]:
    return {a["id"]: a["name"] for a in call("GET", "/api/v1/admin/attorneys", admin=True)[1]}


def offer_by(detail: dict, name: str, names: dict[int, str]) -> dict:
    return next(o for o in detail["offers"] if names[o["attorney_id"]] == name)


def respond(offer: dict, action: str) -> tuple[int, dict]:
    token = offer["link"].split("#t=", 1)[1]
    return call("POST", f"/api/v1/offers/{offer['id']}/{action}", {"token": token})


def main() -> None:
    status, _ = call("GET", "/healthz")
    if status != 200:
        sys.exit(f"Router not reachable at {BASE}. Start it with `make up`.")
    names = attorneys()
    if not names:
        sys.exit("No attorneys yet. Run `make seed` first.")

    step(1, "Five people send in the intake form",
         "Each one takes a different path through the system.")
    jamie = submit(person("Jamie", "Rivera"))
    alex = submit(person("Alex", "Kim", zip_code="60614"))
    taylor = submit(person(
        "Taylor", "Nguyen", practice_area="family_law", injured=None, medical_treatment=None,
        description="My ex and I need to set up a custody schedule for our two kids before school starts.",
        incident_date=None))
    bot = submit(person("Bot", "Spammer", website="http://spam.example",
                        description="cheap pills http://a.example http://b.example"))
    dana = submit(person("Dana", "Lopez", practice_area="immigration", injured=None,
                         description="I need help renewing my work permit before it expires next spring."))
    outcomes = [r["status"] for r in (jamie, alex, taylor, bot, dana)]
    check(outcomes == ["offered", "assigned", "offered", "rejected", "unserviceable"],
          f"five different outcomes, got {outcomes}")

    step(2, "Why did Jamie's lead get the grade it got?",
         "Every point has a written reason, like a teacher's rubric.")
    j = lead(jamie["lead_id"])
    for f in j["score_breakdown"]:
        print(f"  {f['points']:+4d}  {f['reason']}")
    print(f"  {BOLD}= {j['score']}/100, tier {j['tier']}{RESET}")

    step(3, "Ping tree: the highest bidder gets the first chance",
         "Personal injury in 60601 is auctioned. If someone passes, the next bidder is asked.")
    for o in j["offers"]:
        print(f"  round {o['round']}: {names[o['attorney_id']]:<13} ${o['price_cents'] / 100:>6.2f}  {o['status']}")
    print("  Avery passes...")
    respond(offer_by(j, "Avery Stone", names), "decline")
    j = lead(jamie["lead_id"])
    print("  Jordan is now being asked, and accepts.")
    respond(offer_by(j, "Jordan Reyes", names), "accept")
    j = lead(jamie["lead_id"])
    print(f"  {GREEN}Jamie's lead now belongs to {names[j['assigned_attorney_id']]}.{RESET}")
    check(names.get(j["assigned_attorney_id"]) == "Jordan Reyes", "Jordan wins the ping tree")

    step(4, "Exclusive: one attorney owns the ZIP code",
         "Avery rents personal injury in 60614, so Alex's lead went straight to Avery. No one else saw it.")
    a = lead(alex["lead_id"])
    print(f"  Alex -> {names[a['assigned_attorney_id']]} (model: {a['distribution_model']})")
    check(a["distribution_model"] == "exclusive", "Alex's lead is exclusive")

    step(5, "Shared: three attorneys are asked at once, first to click wins",
         "Riley accepts first; Morgan tries a moment later and finds the lead taken. "
         "(Truly simultaneous clicks are proven by tests/test_concurrency_postgres.py.)")
    t = lead(taylor["lead_id"])
    first = respond(offer_by(t, "Riley Chen", names), "accept")
    second = respond(offer_by(t, "Morgan Lee", names), "accept")
    print(f"  Riley clicks accept  -> HTTP {first[0]} ({first[1].get('status')})")
    print(f"  Morgan clicks accept -> HTTP {second[0]} ({second[1].get('detail')})")
    check((first[0], second[0]) == (200, 409), "exactly one attorney wins the shared lead")

    step(6, "Billing: flat fees only",
         "Attorneys pay per lead or a flat monthly fee. Never a share of what they earn.")
    period = date.today().strftime("%Y-%m")
    call("POST", f"/api/v1/admin/billing/run?period={period}", admin=True)
    for attorney_id, name in names.items():
        st = call("GET", f"/api/v1/admin/attorneys/{attorney_id}/statement?period={period}", admin=True)[1]
        if st["total_cents"] or st["entries"]:
            kinds = ", ".join(sorted({e["kind"] for e in st["entries"]}))
            print(f"  {name:<13} ${st['total_cents'] / 100:>9.2f}   ({kinds})")

    step(7, "The dashboard numbers", "The same numbers appear at /admin.")
    s = call("GET", "/api/v1/admin/stats", admin=True)[1]
    print(f"  leads by status:  {s['leads_by_status']}")
    print(f"  leads by model:   {s['leads_by_model']}")
    print(f"  sell-through:     {s['sell_through_rate']}")
    print(f"  revenue {s['billing_period']}:  ${s['revenue_cents_this_month'] / 100:,.2f}")

    if FAILURES:
        sys.exit(f"\n{RED}{len(FAILURES)} expectation(s) failed.{RESET}")
    print(f"\n{GREEN}All demo expectations met.{RESET}")

    print(f"\n{BOLD}Look around:{RESET}")
    print(f"  Dashboard   {BASE}/admin   (admin key is in your .env)")
    print(f"  Inbox       {INBOX}   (every email the system sent)")
    print(f"  API docs    {BASE}/docs")


if __name__ == "__main__":
    main()
