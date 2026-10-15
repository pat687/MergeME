#!/usr/bin/env node
// Browser regression checks for the three pages, run in CI after `make demo`.
//
//   npm install --no-save --prefix /tmp/pw playwright@1.56.1
//   /tmp/pw/node_modules/.bin/playwright install chromium
//   set -a; . ./.env; set +a; NODE_PATH=/tmp/pw/node_modules node scripts/ui_smoke.js
//
// Needs a running stack with demo attorneys loaded and ROUTER_DEMO_MODE=true (so the admin
// API returns attorney links). Exits non-zero if any check fails.

const { chromium } = require("playwright");

const BASE = (process.env.ROUTER_PUBLIC_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const KEY = process.env.ROUTER_ADMIN_API_KEY;
const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok, detail });
let counter = 0;

async function adminGet(path) {
  const res = await fetch(BASE + path, { headers: { "X-Admin-Key": KEY } });
  return res.json();
}

async function newFamilyLead() {
  counter += 1;
  const n = `${process.pid}${counter}`.slice(-4).padStart(4, "0");
  const res = await fetch(`${BASE}/api/v1/intake`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      first_name: "Browser", last_name: "Check", email: `browser.check.${process.pid}.${counter}@example.org`,
      phone: `312444${n}`, zip_code: "60601", state: "IL", practice_area: "family_law",
      description: "My partner and I need help agreeing on a custody schedule for our kids.",
      consent: true, consent_version: (await (await fetch(`${BASE}/api/v1/meta`)).json()).consent_version,
    }),
  });
  const body = await res.json();
  if (res.status !== 201 || body.status !== "offered") throw new Error(`intake failed: ${JSON.stringify(body)}`);
  return adminGet(`/api/v1/admin/leads/${body.lead_id}`);
}

(async () => {
  const browser = await chromium.launch();
  const pageErrors = [];
  const open = async (opts = {}) => {
    const page = await browser.newPage(opts);
    page.on("pageerror", (e) => pageErrors.push(e.message));
    return page;
  };

  // Offer page: cancelling "Pass" really cancels; confirming declines.
  let lead = await newFamilyLead();
  let offer = lead.offers[0];
  let page = await open();
  await page.goto(offer.link);
  await page.click("text=Pass");
  await page.waitForSelector("#pass_reason");
  await page.click("text=Cancel");
  lead = await adminGet(`/api/v1/admin/leads/${lead.id}`);
  check("cancelling Pass keeps the offer pending", lead.offers.find((o) => o.id === offer.id).status === "pending");
  await page.click("text=Pass");
  await page.click("text=Yes, pass on this lead");
  await page.waitForSelector("text=You passed on this lead.");
  lead = await adminGet(`/api/v1/admin/leads/${lead.id}`);
  check("confirming Pass declines the offer", lead.offers.find((o) => o.id === offer.id).status === "declined");

  // Offer page: a double-click on Accept ends accepted, with no error shown.
  offer = lead.offers.find((o) => o.status === "pending");
  await page.goto(offer.link);
  await page.dblclick("text=Accept lead");
  await page.waitForSelector("text=This lead is yours");
  await page.waitForTimeout(800);
  check("double-click Accept shows no error", (await page.$$(".error")).length === 0);

  // Offer page: when the server keeps saying 0s left, the page re-checks gently.
  lead = await newFamilyLead();
  offer = lead.offers[0];
  page = await open();
  let gets = 0;
  await page.route(`**/api/v1/offers/${offer.id}`, async (route) => {
    gets += 1;
    const json = await (await route.fetch()).json();
    await route.fulfill({ json: { ...json, status: "pending", seconds_left: 0 } });
  });
  await page.goto(offer.link);
  await page.waitForTimeout(5000);
  check("countdown at zero doesn't flood the API", gets <= 5, `${gets} requests in 5 s`);

  // Intake form: friendly, accessible per-field errors.
  page = await open({ viewport: { width: 390, height: 844 } });
  await page.goto(`${BASE}/`);
  await page.waitForSelector("#submit:not([disabled])");
  await page.check("[name=consent]");
  await page.click("#submit");
  await page.waitForSelector(".field-error");
  const messages = (await page.$$eval(".field-error", (els) => els.map((e) => e.textContent))).join(" ");
  const invalid = await page.$$eval("[aria-invalid=true]", (els) => els.length);
  const focused = await page.evaluate(() => document.activeElement?.name);
  check("intake shows friendly field errors", invalid >= 5 && focused === "first_name" && !/Value error|Input should/.test(messages),
    JSON.stringify({ invalid, focused }));

  // Intake form: Enter before the form's settings load must not submit natively
  // (that would put the person's details in the page URL).
  page = await open();
  await page.route("**/api/v1/meta", async (route) => {
    await new Promise((r) => setTimeout(r, 3000));
    await route.continue();
  });
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.fill("[name=first_name]", "Jane");
  await page.press("[name=first_name]", "Enter");
  await page.waitForTimeout(500);
  check("no native submit before the form loads", !page.url().includes("?"), page.url());

  // Dashboard at phone width: keyboard access, no sideways scroll, refresh keeps focus.
  page = await open({ viewport: { width: 390, height: 900 } });
  await page.goto(`${BASE}/admin`);
  await page.fill("#key", KEY);
  await page.click("#login_form button");
  await page.waitForSelector("#rows tr");
  await page.focus("#rows tr:first-child");
  await page.keyboard.press("Enter");
  await page.waitForSelector("#detail .timeline li");
  check("Enter on a lead row opens it and highlights it", (await page.$$("#rows tr.selected")).length === 1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check("dashboard has no sideways scroll at 390px", overflow <= 0, `${overflow}px too wide`);
  const link = await page.$("#detail a");
  if (link) {
    await link.focus();
    await page.waitForTimeout(6500);  // longer than the 5 s auto-refresh
    check("auto-refresh keeps focus in the detail panel",
      await page.evaluate(() => document.getElementById("detail").contains(document.activeElement)));
  }

  check("no JavaScript errors on any page", pageErrors.length === 0, pageErrors.join(" | "));
  await browser.close();

  for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}${r.ok || !r.detail ? "" : `  (${r.detail})`}`);
  const failed = results.filter((r) => !r.ok).length;
  console.log(failed ? `\n${failed} browser check(s) failed` : `\nAll ${results.length} browser checks passed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
