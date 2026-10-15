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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tokenOf = (link) => new URL(link).hash.replace("#t=", "");

async function newFamilyLead() {
  counter += 1;
  const n = `${process.pid}${counter}`.slice(-4).padStart(4, "0");
  const consentVersion = (await (await fetch(`${BASE}/api/v1/meta`)).json()).consent_version;
  let res;
  // The intake limit is per IP per minute and `make demo` just used some of it: wait it out.
  for (let tries = 0; tries < 15; tries += 1) {
    res = await fetch(`${BASE}/api/v1/intake`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        first_name: "Browser", last_name: "Check", email: `browser.check.${process.pid}.${counter}@example.org`,
        phone: `312444${n}`, zip_code: "60601", state: "IL", practice_area: "family_law",
        description: "My partner and I need help agreeing on a custody schedule for our kids.",
        consent: true, consent_version: consentVersion,
      }),
    });
    if (res.status !== 429) break;
    await sleep(5000);
  }
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

  // Offer page: cancelling "Pass" (button or Escape) really cancels and returns focus to
  // Pass; Enter in the reason box confirms.
  let lead = await newFamilyLead();
  let offer = lead.offers[0];
  let page = await open();
  await page.goto(offer.link);
  await page.waitForSelector("#pass");
  check("offer status is announced once, not the ticking countdown",
    (await page.$eval("#view", (el) => el.getAttribute("aria-live"))) === null
      && (await page.textContent("#status")).includes("Accept to receive"));
  await page.click("#pass");
  await page.waitForSelector("#pass_reason");
  await page.keyboard.press("Escape");
  const afterEscape = await page.evaluate(() => [!!document.getElementById("pass_reason"), document.activeElement?.id]);
  await page.click("#pass");
  await page.click("text=Cancel");
  const afterCancel = await page.evaluate(() => [!!document.getElementById("pass_reason"), document.activeElement?.id]);
  lead = await adminGet(`/api/v1/admin/leads/${lead.id}`);
  check("Cancel and Escape keep the offer pending and focus Pass",
    lead.offers.find((o) => o.id === offer.id).status === "pending"
      && JSON.stringify([afterEscape, afterCancel]) === JSON.stringify([[false, "pass"], [false, "pass"]]),
    JSON.stringify([afterEscape, afterCancel]));
  await page.click("#pass");
  await page.fill("#pass_reason", "conflict");
  await page.press("#pass_reason", "Enter");
  await page.waitForSelector("text=You passed on this lead.");
  lead = await adminGet(`/api/v1/admin/leads/${lead.id}`);
  check("Enter in the reason box confirms the pass", lead.offers.find((o) => o.id === offer.id).status === "declined");

  // Offer page: a bad link's error is announced, not just shown. (Start from a blank
  // page and a different offer, so this is always a full page load, not a #hash change.)
  await page.goto("about:blank");
  await page.goto(`${BASE}/offer/${lead.offers.find((o) => o.id !== offer.id).id}#t=not-a-real-token`);
  await page.waitForSelector("#view .error");
  check("a link that fails to load is announced to screen readers",
    (await page.textContent("#status")).includes("not found"));

  // Offer page: an old-style '?t=' link still works, even with a '#' part, and the token
  // is moved out of the address so it isn't sent to the server again.
  const old = lead.offers.find((o) => o.status === "pending");
  await page.goto(`${BASE}/offer/${old.id}?t=${tokenOf(old.link)}#section`);
  const loaded = await page.waitForSelector("#pass", { timeout: 5000 }).catch(() => null);
  check("old ?t= links keep working", loaded !== null && !page.url().includes("?t="), page.url());

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
  // State and matter type come first, so the first field to fix is the ZIP code.
  check("intake shows friendly field errors", invalid >= 5 && focused === "zip_code" && !/Value error|Input should/.test(messages),
    JSON.stringify({ invalid, focused }));
  // The specific reason is shown when our own check has one, and read once (as the
  // field's description, not also as part of its label).
  await page.fill("[name=first_name]", "J.R");
  await page.click("#submit");
  await page.waitForSelector("#error_first_name");
  const nameError = await page.evaluate(() => {
    const input = document.querySelector("[name=first_name]");
    const span = document.getElementById("error_first_name");
    return { text: span.textContent, hidden: span.getAttribute("aria-hidden"), described: input.getAttribute("aria-describedby") };
  });
  check("name errors give the real reason, read once",
    /period must be followed by a space/.test(nameError.text) && nameError.hidden === "true"
      && nameError.described.includes("error_first_name"), JSON.stringify(nameError));

  // Intake form: says up front when a state or matter isn't accepted (before anyone
  // types details), asks injury questions and the separate health consent only where
  // they apply, and writes the typed phone number into the consent.
  const meta = await (await fetch(`${BASE}/api/v1/meta`)).json();
  // A state this stack doesn't accept, and one where injury matters are open.
  const closedState = ["WY", "VT", "ND", "WA", "WI"].find((st) => !(st in meta.open));
  const openState = Object.keys(meta.open).find((st) => meta.open[st].includes("personal_injury"));
  page = await open({ viewport: { width: 390, height: 844 } });
  await page.goto(`${BASE}/`);
  await page.waitForSelector("#submit:not([disabled])");
  await page.fill("[name=state]", closedState);
  await page.selectOption("#practice_area", "personal_injury");
  const closedForm = await page.evaluate(() => ({
    notice: document.getElementById("availability").textContent,
    detailsHidden: document.getElementById("details").hidden,
    submitDisabled: document.getElementById("submit").disabled,
  }));
  await page.fill("[name=state]", openState);
  // What a person actually sees, not just the hidden attribute.
  const injuryForm = {
    detailsHidden: !(await page.isVisible("#details")),
    injury: await page.isVisible("#injury_questions"),
    sensitive: await page.isVisible("#sensitive_wrap"),
  };
  await page.selectOption("#practice_area", "family_law");
  const familyForm = {
    injury: await page.isVisible("#injury_questions"),
    sensitive: await page.isVisible("#sensitive_wrap"),
  };
  await page.fill("[name=phone]", "312 555 0199");
  const consentShown = await page.textContent("#consent_text");
  check("intake says which states and matters it can't take, before details are typed",
    closedForm.notice.includes(`aren't accepting inquiries from ${closedState}`) && closedForm.detailsHidden
      && closedForm.submitDisabled && !injuryForm.detailsHidden,
    JSON.stringify(closedForm));
  check("injury questions and the health consent appear only for injury matters; consent names the number",
    injuryForm.injury && injuryForm.sensitive && !familyForm.injury && !familyForm.sensitive
      && consentShown.includes("(312) 555-0199"),
    JSON.stringify({ injuryForm, familyForm, consentShown: consentShown.slice(0, 160) }));

  // Soft launch (no state open yet): the form says so as soon as it loads.
  page = await open({ viewport: { width: 390, height: 844 } });
  await page.route("**/api/v1/meta", async (route) => {
    const res = await route.fetch();
    await route.fulfill({ response: res, json: { ...(await res.json()), open: {} } });
  });
  await page.goto(`${BASE}/`);
  await page.waitForFunction(() => document.getElementById("availability").textContent);
  const softLaunch = await page.evaluate(() => ({
    notice: document.getElementById("availability").textContent,
    detailsVisible: !!document.getElementById("details").offsetParent,
    submitDisabled: document.getElementById("submit").disabled,
  }));
  check("with no state open yet, the form says so on load and can't be sent",
    softLaunch.notice.startsWith("Sorry, we aren't accepting inquiries yet.") && !softLaunch.detailsVisible
      && softLaunch.submitDisabled, JSON.stringify(softLaunch));

  page = await open();
  await page.goto(`${BASE}/privacy`);
  await page.waitForFunction(() => document.querySelector("[data-fill=rejected]")?.textContent);
  const policy = await page.evaluate(() => ({
    rejected: document.querySelector("[data-fill=rejected]").textContent,
    email: document.querySelector("[data-fill=privacy_link]").textContent,
  }));
  check("privacy policy fills in the real retention periods and contact",
    policy.rejected === String(meta.retention_days.rejected) && policy.email === meta.privacy_email,
    JSON.stringify(policy));

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

  // Dashboard at phone width: keyboard access, no sideways scroll, refresh keeps focus
  // and scroll position, and the open lead keeps updating.
  lead = await newFamilyLead();
  page = await open({ viewport: { width: 390, height: 900 } });
  await page.goto(`${BASE}/admin`);
  await page.fill("#key", KEY);
  await page.click("#login_form button");
  await page.waitForSelector(`#rows button[data-id="${lead.id}"]`);
  check("lead rows keep table semantics and use a real button",
    (await page.$$("#rows tr[role]")).length === 0 && (await page.$$("#rows button.rowlink")).length > 0);
  await page.focus(`#rows button[data-id="${lead.id}"]`);
  await page.keyboard.press("Enter");
  await page.waitForSelector("#detail .timeline li");
  check("Enter on a lead row opens it and highlights it", (await page.$$("#rows tr.selected")).length === 1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check("dashboard has no sideways scroll at 390px", overflow <= 0, `${overflow}px too wide`);

  // Focus a row button, scroll away, and let a new lead arrive: the rows are rebuilt,
  // but focus stays put and the page doesn't jump back.
  await page.focus(`#rows button[data-id="${lead.id}"]`);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const scrolledTo = await page.evaluate(() => window.scrollY);
  const arriving = await newFamilyLead();
  await page.waitForSelector(`#rows button[data-id="${arriving.id}"]`, { timeout: 8000 });
  const after = await page.evaluate(() => [window.scrollY, document.activeElement?.dataset?.id]);
  // (The browser may nudge scrollY down by a row height to keep the content in view;
  // the bug this guards against jumped the page back up to the focused row.)
  check("auto-refresh keeps focus and doesn't scroll the page",
    after[0] >= scrolledTo && after[1] === lead.id, JSON.stringify({ scrolledTo, after }));

  // A focused row that is on screen, a few rows down, must not jump either. (Removing
  // the focused button while rebuilding makes the browser re-anchor the page.) Needs a
  // few rows, so add leads on a stack that doesn't have them yet.
  while ((await page.$$("#rows tr")).length < 6) {
    const extra = await newFamilyLead();
    await page.waitForSelector(`#rows button[data-id="${extra.id}"]`, { timeout: 8000 });
  }
  const rowId = await page.evaluate(() => {
    const button = document.querySelector("#rows tr:nth-child(5) button.rowlink");
    button.focus({ preventScroll: true });
    window.scrollTo(0, window.scrollY + button.getBoundingClientRect().top - 80);
    return button.dataset.id;
  });
  const rowTop = (id) => page.evaluate((i) => [window.scrollY,
    document.querySelector(`#rows button[data-id="${i}"]`).getBoundingClientRect().top,
    document.activeElement?.dataset?.id], id);
  const rowBefore = await rowTop(rowId);
  const another = await newFamilyLead();
  await page.waitForSelector(`#rows button[data-id="${another.id}"]`, { timeout: 8000 });
  const rowAfter = await rowTop(rowId);
  check("a focused row on screen stays put when rows are rebuilt",
    Math.abs(rowAfter[1] - rowBefore[1]) <= 50 && rowAfter[0] >= rowBefore[0] && rowAfter[2] === rowId,
    JSON.stringify({ rowBefore, rowAfter }));

  // Focus a link inside the detail panel (on a phone this also scrolls the offers table
  // sideways), then change the lead: the panel updates, focus stays, nothing moves.
  // (The lead opened above is still the one shown in the panel.)
  await page.locator("#detail a").first().focus();
  // Put the focused link near the top of the screen: that's where a rebuild used to jump.
  await page.evaluate(() => window.scrollBy(0, document.activeElement.getBoundingClientRect().top - 60));
  const place = () => page.evaluate(() => {
    const f = document.activeElement, w = f?.closest(".table-wrap");
    const inView = !!w && f.getBoundingClientRect().left >= w.getBoundingClientRect().left - 1
      && f.getBoundingClientRect().right <= w.getBoundingClientRect().right + 1;
    return [window.scrollY, inView];
  });
  const placeBefore = await place();
  const first = lead.offers[0];
  await fetch(`${BASE}/api/v1/offers/${first.id}/decline`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: tokenOf(first.link), reason: "smoke-test pass reason" }),
  });
  await page.waitForSelector("#detail >> text=smoke-test pass reason", { timeout: 8000 }).catch(() => null);
  const placeAfter = await place();
  check("open lead keeps updating, with focus kept in the panel",
    (await page.$("#detail >> text=smoke-test pass reason")) !== null
      && await page.evaluate(() => document.getElementById("detail").contains(document.activeElement)));
  check("updating the open lead doesn't move the page, and the focused link stays visible",
    Math.abs(placeAfter[0] - placeBefore[0]) <= 2 && placeAfter[1] === true,
    JSON.stringify({ placeBefore, placeAfter }));

  // Opening a different lead starts its offers table at the left edge, not wherever the
  // previous lead's table had been scrolled to.
  const sidewaysBefore = await page.evaluate(() => document.querySelector("#detail .table-wrap").scrollLeft);
  await page.focus(`#rows button[data-id="${another.id}"]`);
  await page.keyboard.press("Enter");
  await page.waitForSelector(`#detail a[href*="${another.offers[0].id}"]`);  // the new lead is shown
  const sidewaysAfter = await page.evaluate(() => document.querySelector("#detail .table-wrap")?.scrollLeft ?? 0);
  check("a different lead's offers table starts at the left edge",
    sidewaysBefore > 0 && sidewaysAfter === 0, JSON.stringify({ sidewaysBefore, sidewaysAfter }));

  // Bid page: an attorney sees where they rank, gets a clear reason when a bid is too
  // low, and moves to first place by raising it. Keyboard focus survives both saves.
  // A throwaway attorney bidding $70 like Sam, so the demo attorneys' bids (and their daily
  // limit on bid saves) are left alone; it's deactivated again at the end.
  const bidder = await (await fetch(`${BASE}/api/v1/admin/attorneys`, {
    method: "POST", headers: { "X-Admin-Key": KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      name: `Browser Bidder ${process.pid}`, firm: "Example Firm (Demo)",
      email: `browser.bidder.${process.pid}@example.com`, bar_states: ["IL"],
      service_zips: ["606"], bids_cents: { personal_injury: 7000 },
    }),
  })).json();
  await fetch(`${BASE}/api/v1/admin/attorneys/${bidder.id}/license-check`, {  // as at onboarding
    method: "POST", headers: { "X-Admin-Key": KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ license_numbers: { IL: "TEST" } }),
  });
  const bidLink = (await (await fetch(`${BASE}/api/v1/admin/attorneys/${bidder.id}/bid-link`,
    { method: "POST", headers: { "X-Admin-Key": KEY } })).json()).link;
  page = await open({ viewport: { width: 390, height: 900 } });
  await page.goto(bidLink);
  await page.waitForSelector("#bid_personal_injury");
  const piCell = (n) => page.$eval("#bid_personal_injury", (el, i) => el.closest("tr").cells[i].textContent, n);
  const rankBefore = await piCell(2);
  const firstBefore = await piCell(3);  // Avery bids $95: shown rounded up to $100, never exact
  await page.fill("#bid_personal_injury", "10");
  await page.focus("#save");
  await page.keyboard.press("Enter");
  await page.waitForSelector("#message.error");
  const tooLow = await page.textContent("#message");
  const refusedFocus = await page.evaluate(() =>
    [document.activeElement.id, document.activeElement.getAttribute("aria-invalid")].join(" "));
  await page.fill("#bid_personal_injury", "99");
  await page.focus("#save");
  await page.keyboard.press("Enter");
  await page.waitForSelector("text=Saved.");
  const savedFocus = await page.evaluate(() => document.activeElement.id);
  const rankAfter = await piCell(2);
  const firstAfter = await piCell(3);
  const bidOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check("attorney bid page explains the floor and a higher bid moves them to first",
    /lowest bid accepted/.test(tooLow) && !rankBefore.startsWith("#1 ") && rankAfter.startsWith("#1 ")
      && firstBefore === "$100.00" && firstAfter === "You're first" && bidOverflow <= 0,
    JSON.stringify({ tooLow, rankBefore, rankAfter, firstBefore, firstAfter, bidOverflow }));
  check("bid page keeps keyboard focus: on the refused field, then on Save",
    refusedFocus === "bid_personal_injury true" && savedFocus === "save",
    JSON.stringify({ refusedFocus, savedFocus }));

  // Subscribers' leads are included in their fee, so a bid would cost them nothing: their
  // page shows the market but can't be changed.
  const morgan = (await adminGet("/api/v1/admin/attorneys")).find((a) => a.name === "Morgan Lee");
  const morganLink = (await (await fetch(`${BASE}/api/v1/admin/attorneys/${morgan.id}/bid-link`,
    { method: "POST", headers: { "X-Admin-Key": KEY } })).json()).link;
  page = await open({ viewport: { width: 390, height: 900 } });
  await page.goto(morganLink);
  await page.waitForSelector("#bid_family_law");
  const readOnly = await page.$eval("#bid_family_law", (el) => el.readOnly);
  check("a subscriber's bid page is read-only", readOnly && (await page.$("#save")) === null,
    JSON.stringify({ readOnly }));

  // Dashboard market panel: a refresh with new data doesn't wipe text being selected.
  page = await open({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/admin`);
  await page.fill("#key", KEY);
  await page.click("#login_form button");
  await page.click("#market summary");
  await page.waitForSelector("#market_rows tr");
  const selectBidders = () => page.evaluate(() => {
    getSelection().selectAllChildren(document.querySelector("#market_rows tr td:nth-child(4)"));
    return getSelection().toString();
  });
  const selectedBefore = await selectBidders();
  await fetch(`${BASE}/api/v1/admin/attorneys/${bidder.id}`, {  // retire it; the market changes
    method: "PATCH", headers: { "X-Admin-Key": KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ active: false }),
  });
  await page.waitForTimeout(6000);
  const selectedAfter = await page.evaluate(() => getSelection().toString());
  check("market panel refresh keeps text the operator is selecting",
    selectedBefore.length > 0 && selectedAfter === selectedBefore, JSON.stringify({ selectedBefore, selectedAfter }));

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
