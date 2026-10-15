# Compliance by design

> **Not legal advice.** This document explains how the software is built to support
> compliance. Attorney-advertising, lead-generation, telemarketing, privacy and
> insurance-fraud rules vary by state and change over time. Have a lawyer licensed in each
> state you operate in review the business model, the consumer-facing wording and your
> contracts before launch.
>
> **About the sources.** The state-by-state rules below come from a legal audit made in
> October 2026. Its researchers could not open the official texts (bar sites, legislatures)
> from their sandbox, so every citation was checked only against search summaries or
> memory. Treat them as a list of questions for counsel, not as answers. The full issue map
> for counsel is [`docs/legal/COUNSEL_BRIEF.md`](legal/COUNSEL_BRIEF.md).

Related documents:

| Document | For |
|---|---|
| [`legal/COUNSEL_BRIEF.md`](legal/COUNSEL_BRIEF.md) | Your lawyer: the decisions needed, a state-by-state map, practice-area rules |
| [`legal/ADVERTISING_AND_OUTREACH_RULES.md`](legal/ADVERTISING_AND_OUTREACH_RULES.md) | Whoever runs ads, the Facebook page, the website and cold email to attorneys |
| [`legal/AGREEMENT_OUTLINES.md`](legal/AGREEMENT_OUTLINES.md) | Counsel: what the attorney agreement, consumer terms and vendor contracts must cover |
| [`legal/PRIVACY_AND_SECURITY_PROGRAM.md`](legal/PRIVACY_AND_SECURITY_PROGRAM.md) | Staff: retention schedule, privacy requests, legal holds, incident response, security |

## 1. What kind of business this software assumes

Under the ABA Model Rules, a lawyer may pay for **advertising**, including paying a lead
generator, but may not pay someone for **recommending** them, and may not **share legal fees**
with a non-lawyer. A for-profit platform that isn't approved by a regulator as a
"qualified lawyer referral service" should therefore operate as **attorney advertising /
lead generation**, and must not present itself as a referral service that recommends lawyers.
Some states go further: they regulate or forbid for-profit services that pass inquiries to
paying lawyers, or make paid referrals of insurance claims a crime (section 2).

| Principle | Source |
|---|---|
| Lawyers may pay the reasonable cost of advertising, including lead generation | ABA Model Rule 7.2(b)(1) |
| A lead generator must not recommend the lawyer, imply the referral is free, or imply it analyzed the person's legal problem to choose the lawyer | ABA Model Rule 7.2, Comment [5]; Rule 7.1 |
| Payments must be consistent with the fee-division and independence rules | ABA Model Rules 1.5(e) and 5.4(a) |
| Lawyers may pay a *qualified* (regulator-approved) lawyer referral service | ABA Model Rule 7.2(b)(2) |
| Communications must not be false or misleading | ABA Model Rule 7.1 |

## 2. Where leads may be sold

Nothing is sold anywhere until a state is listed in `ROUTER_LAUNCH_STATES`. Choose those
states with counsel. On top of that, a built-in table (`compliance.RESTRICTIONS`) lists the
states and practice areas the audit flagged. Leads covered by it are refused unless
`ROUTER_COUNSEL_CLEARED` records counsel's written approval ("ST" for the whole state, or
"ST:practice_area"). The service refuses to start if a launched state has a whole-state
restriction that hasn't been cleared. `ROUTER_BLOCKED_AREAS_BY_STATE` adds your own blocks.

| Flagged | Why (unverified; see the counsel brief) |
|---|---|
| California (all) | Lawyer referral services must be certified by the State Bar (Bus. & Prof. Code 6155; *Jackson v. LegalMatch.com*); SB 37 (2025); workers' comp and insurance anti-referral laws |
| Texas (all) | Barratry (Penal Code 38.12); for-profit referral services can't be certified (Occ. Code ch. 952); per-lead pricing outside Ethics Op. 573 |
| Colorado (all) | SB26-174 (2026) makes paying for legal leads a deceptive trade practice |
| Michigan (all) | Per-lead fees treated as paying for a recommendation (Informal Op. RI-365) |
| Ohio, Florida, Georgia, New Jersey, Kentucky, South Carolina (all) | Referral-service registration, filing or bans; Florida "qualifying provider" rules; South Carolina closed-panel concerns |
| Louisiana (all) | Criminal ban on paying to obtain representation (La. R.S. 37:219) |
| Illinois, Pennsylvania (injury areas) | Insurance-claim referral statutes (740 ILCS 92/5; 18 Pa.C.S. 4117(b)) |
| Washington, Nevada (injury areas) | Selling consumer health data needs a signed authorization naming the buyer |
| Connecticut, Maryland (injury areas and immigration) | Sensitive and consumer health data consent rules; Maryland bans selling sensitive data |
| North Carolina, District of Columbia (workers' comp) | Workers' comp solicitation and fee statutes |
| Federal and maritime workers' comp (everywhere) | Claims under federal programs: refused on the form |

About 30 other states were not researched one by one. "Not flagged" does not mean "cleared".

Inquiries that can't be accepted are refused **before anything is stored**: the form asks
for the state and type of matter first and says so straight away, and the server keeps
nothing and sends nothing, only pointing the person to their bar's lawyer referral service.

## 3. Rule → feature → proof

| Requirement | How the code meets it | Enforced in | Tested in |
|---|---|---|---|
| **No fee-splitting.** Never take a share of an attorney's fee | The ledger has four flat charge kinds (`per_lead`, `included`, `subscription`, `territory`). There is no percentage or outcome kind to choose. | [`domain/types.py`](../services/lead-router/app/domain/types.py) `ChargeKind` | `test_there_is_no_charge_kind_tied_to_case_outcomes` |
| Every charge is whole, non-negative cents | Single gate `assert_flat_fee` on every ledger insert | [`domain/compliance.py`](../services/lead-router/app/domain/compliance.py) | `test_ledger_gate_refuses_anything_but_flat_non_negative_cents` |
| Percentage pricing can't sneak in through the API | Attorney schemas use `extra="forbid"`: a `fee_percent` field is rejected with 422 | [`schemas.py`](../services/lead-router/app/schemas.py) `AttorneyBase` | `test_percentage_pricing_cannot_even_be_submitted` |
| **Price doesn't depend on case outcome** | Charges happen at delivery (accept, or exclusive assignment). There is no endpoint to report retention, settlement or recovery. | [`services/billing.py`](../services/lead-router/app/services/billing.py) | `test_ping_tree_walks_down_the_line...` (charge on accept) |
| **Neutral, disclosed selection.** Opinions that allow paying a lead generator ask that attorneys be chosen by neutral, mechanical criteria that are disclosed, without a recommendation ([NYSBA Op. 1131](https://nysba.org/ethics-opinion-1131/), [NYSBA Op. 1294](https://nysba.org/ethics-opinion-1294-solicitation-advertisement-lead-generators/); [WSBA Advisory Op. 201401](https://ao.wsba.org/print.aspx?ID=1680) asks that the *consumer* be told) | Who can receive a lead is decided only by objective facts (state, ZIP, practice area, capacity, a checked license) and the order by a mechanical rule: in auctions, highest paying bid first, then subscribers; in shared areas, fewest leads today first, then paying attorneys by price, then subscribers. The consumer disclosure now says so, including that attorneys who pay more may be offered it first and that one attorney may rent a ZIP code. **Open for counsel:** whether price priority counts as "neutral" in each launch state. | `distribution`, `compliance.disclosure()` | `test_disclosure_explains_selection_paid_priority_and_territories` |
| **No recommending.** Don't imply we chose the "best" lawyer | Consumer text says attorneys *pay*, that we *do not recommend* any attorney, and that no one at the platform reviews the matter | `compliance.disclosure()`, `consumer_message()`, `consent_text()` | `test_no_consumer_facing_text_claims_a_recommendation` |
| Don't imply the referral is free to the lawyer | The disclosure and the consent both say attorneys pay us | `compliance.disclosure()`, `consent_text()` | same |
| Don't imply we analyzed their legal problem | The score is an internal quality grade shown only to attorneys and staff; it never rejects a lead, never changes who receives it or its price, and is never shown to consumers | [`domain/scoring.py`](../services/lead-router/app/domain/scoring.py) docstring | n/a (by construction) |
| Not a law firm / no attorney-client relationship | Disclosure on the form, in the API response and in the receipt email | `compliance.disclosure()`, `emails.consumer_receipt` | `test_consumer_wording_never_claims_to_recommend` |
| "Attorney advertising" label | Header tag and first words of the disclosure | [`static/intake.html`](../services/lead-router/app/static/intake.html) | n/a |
| Responsible lawyer named where a state requires it | `ROUTER_RESPONSIBLE_ATTORNEYS` shows a per-state line on the form once the state is entered. **Counsel decides** whether and whom to name. | `intake.html` | n/a |
| Attorney independence (Rule 5.4) | Attorneys decide to accept or pass on every lead; flags like "time-sensitive" are notes, never auto-rejections | `scoring._flags`, `offers.accept/decline` | `test_old_incident_gets_deadline_flag_but_is_not_rejected` |
| **Only licensed attorneys** | No leads without a bar-license check recorded within `ROUTER_LICENSE_RECHECK_DAYS` (90), checked when the line-up is made, when each attorney's turn comes and at acceptance; staff record it with the bar numbers from each state bar's site | `offers.credentials_problem` | `test_attorneys_without_a_recent_license_check_get_no_leads`, `test_a_license_lapse_is_caught_when_the_attorneys_turn_comes` |
| Attorneys agreed to the rules | Once `ROUTER_ATTORNEY_AGREEMENT_VERSION` is set, no leads until that version's acceptance is recorded | `offers.credentials_problem` | `test_once_published_the_agreement_must_be_accepted` |
| Bankruptcy: 11 U.S.C. 528 statement; immigration: no notario impression | Shown on the form when those matters are chosen | `compliance.area_notices` | `test_area_notices_cover_immigration_and_bankruptcy` |
| Federal and maritime workers' comp | Asked on the form; such claims are refused and pointed to the U.S. Department of Labor | `IntakeRequest`, `pipeline.refusal` | `test_federal_and_maritime_workers_comp_claims_are_not_taken` |

## 4. Consumer consent and contact (TCPA and state telemarketing laws)

Attorneys will call and text consumers, so consent evidence matters.

| Control | Detail |
|---|---|
| Explicit opt-in | Unchecked checkbox. The server rejects any lead without `consent=true` (`scrubbing.scrub`). |
| Consumer-directed | The consent is the person's request: "Please send my inquiry to up to N participating attorneys...", and says those attorneys pay us. |
| The number is named | The consent says calls and texts go to the number entered; the form shows that number in the sentence as it's typed, and the stored text contains it. |
| Attestations | "I am 18 or older, and the phone number and email I entered are mine or ones I regularly use." |
| Separate consent for sensitive details | Injury areas (health) and immigration need a second, unchecked box. Without it the inquiry is refused before anything is stored. Its wording is stored on the lead (`sensitive_consent_text`). |
| Exact wording stored | `leads.consent_text` (with the number) and `consent_version`. A submission whose version doesn't match the wording currently shown is rejected ("please reload"), so the two always agree. The receipt email gives the person a copy. |
| When and from where | `consent_at`, `ip_address`, `user_agent`, the Global Privacy Control signal, and where the visit came from (utm tags, referring site, landing page), as evidence that the person responded to advertising rather than to outreach. The IP comes from the connection; `X-Forwarded-For` is honored only from configured proxies (`ROUTER_TRUSTED_PROXY_IPS`). |
| Bounded sharing | The consent names the maximum number of attorneys (`ROUTER_SHARED_MAX_ATTORNEYS`), stored per lead, and every model respects it. Every attorney who received the lead is in its audit trail. |
| The person learns who has their details | When an attorney accepts (or an exclusive owner receives it), the person is emailed the attorney's name, firm and contact details, how to stop contact and how to ask for deletion. |
| Not a condition of purchase; how to stop | Both in the consent text. |

Notes for counsel:
- The consent does not name the attorney before submission. The FCC's 2023 "one-to-one
  consent" rule was vacated (*Insurance Marketing Coalition v. FCC*, 11th Cir. 2025), but
  the Do-Not-Call exception still refers to a written agreement with "this seller", and
  Washington's health-data law needs the buyer named. A two-step flow that names the
  attorneys before final consent is the stronger design; it is not built.
- State telemarketing statutes (for example Florida's FTSA) have stricter consent,
  calling-hour and frequency rules. Calling is done by attorneys; the attorney agreement
  must put those duties on them (`legal/AGREEMENT_OUTLINES.md`).

## 5. Privacy and confidentiality

| Concern | Control |
|---|---|
| Notice at collection and privacy policy | A notice under the consent boxes links to `/privacy`, a policy generated from the real settings (what is collected, who receives it, retention periods, rights, contact). **Counsel must approve it before launch.** |
| Prospective-client information (Rule 1.18) | Offer previews and offer emails withhold the name, phone and email, and the description is redacted first (best-effort pattern matching). The form tells people to describe things in general terms and warns that an attorney who sees the inquiry may represent someone else in the matter. Exclusive-territory owners receive full details at once. |
| Minimum necessary | Injury questions only in injury areas; nothing is kept for refused inquiries; eXo receives no consumer contact details or description unless `ROUTER_EXO_SYNC_CONTACT_DETAILS=true`. |
| Access and deletion requests | `GET /api/v1/admin/privacy/lookup` finds and exports everything about a person. `POST /api/v1/admin/leads/{id}/erase` erases at two levels: `narrative` (the story, health and matter details, browser, marketing fields, attorneys' pass reasons, and every email copy) or `full` (also name, contact details and IP). An erased lead still on offer is withdrawn, and attorneys who received it are asked to delete it. |
| Retention | The worker erases on a schedule (`ROUTER_RETENTION_*`): rejected submissions after 30 days, unsold or unserviceable after 90, the story of sold leads after 90, and their consent evidence after about 5 years 3 months; sent email bodies after 14 days. See `legal/PRIVACY_AND_SECURITY_PROGRAM.md`. |
| Legal holds | `POST /api/v1/admin/leads/{id}/legal-hold` stops erasing and the schedule for that lead until lifted. |
| Selling personal information (CCPA/CPRA and other states) | Attorneys pay for inquiries, which some laws may treat as a "sale". The flow is built as a consumer-directed disclosure (the consent is the person's request), the privacy page says so, and the Global Privacy Control signal is recorded. **Counsel decides** whether that is enough in each state or an opt-out and further notices are needed. |
| Health-data states | Injury areas in WA, NV, CT and MD are flagged (section 2), so health-related inquiries from there are refused unless counsel clears them. |
| Access control | Admin endpoints require `X-Admin-Key`; attorneys only see offers through signed links. Put `/admin` and `/docs` behind single sign-on with MFA at your reverse proxy in production. |
| Transport and storage | Production mode refuses to start without an `https://` public URL or with the Mailpit test inbox. Encryption at rest and encrypted off-site backups are a hosting task (see the security program). |

## 6. When nobody can help

If no participating attorney covers the request, or the state or matter isn't open, the
person is told so plainly and pointed to their state or local bar association's lawyer
referral service (`LeadStatus.UNSERVICEABLE`, `compliance.consumer_message`). No dead ends,
and no pretending.

## 7. Launch checklist (business and legal)

- [ ] Counsel in each launch state reviews and approves in writing: the business model and
      ordering rule, the disclosure, the consent and sensitive-data consent wording, the
      privacy policy, and any registration or filing (see the counsel brief)
- [ ] Set `ROUTER_LAUNCH_STATES`, and `ROUTER_COUNSEL_CLEARED` only for what counsel approved
- [ ] Insurance-fraud, runner and barratry statutes cleared for every launch state and
      practice area (the brief's "gap" findings), starting with injury areas
- [ ] Attorney participation agreement signed by every attorney; set
      `ROUTER_ATTORNEY_AGREEMENT_VERSION` and record each acceptance
- [ ] Bar-license check recorded for every attorney, and repeated at least every 90 days
- [ ] Consumer Terms of Use, and vendor contracts (email, hosting, payments)
- [ ] `ROUTER_LEGAL_ENTITY_NAME`, `ROUTER_POSTAL_ADDRESS`, `ROUTER_PRIVACY_EMAIL` set; a
      monitored privacy mailbox
- [ ] Responsible-lawyer naming and ad filing per state (`ROUTER_RESPONSIBLE_ATTORNEYS`)
- [ ] Advertising, social media and cold-email rules adopted by everyone who markets
      (`legal/ADVERTISING_AND_OUTREACH_RULES.md`)
- [ ] Security program in place: TLS, SSO/MFA for `/admin` and `/docs`, encrypted disks and
      backups, incident response plan (`legal/PRIVACY_AND_SECURITY_PROGRAM.md`)
- [ ] Sales-tax treatment of lead sales and subscriptions checked with an accountant
- [ ] Trademark clearance for the names you use
- [ ] Keep every old wording version on file (leads store which version they agreed to;
      section 8)

## 8. Changing the wording safely

All consumer-facing compliance wording lives in one file,
[`domain/compliance.py`](../services/lead-router/app/domain/compliance.py); the intake page
([`static/intake.html`](../services/lead-router/app/static/intake.html)) only adds headings,
field hints and the notice at collection, and [`static/privacy.html`](../services/lead-router/app/static/privacy.html)
holds the privacy policy. To change any of them:

1. Edit the text and bump `ROUTER_DISCLOSURE_VERSION` (and the privacy page's date).
2. Run `make test`. `test_no_consumer_facing_text_claims_a_recommendation` checks the
   disclosure, the consent text, every post-submission message, the receipt email's
   subject and body, and the intake page's text for phrases like "best", "top-rated",
   "matched you" or "evaluated your case".
3. Archive the old wording below. Existing leads keep the exact text they agreed to.

### Archive

**2026-10-01** (replaced 2026-10-15)

- Disclosure: "Attorney advertising. {platform} is not a law firm and does not give legal
  advice. Participating attorneys pay a flat fee to receive inquiries like yours. We do not
  recommend or endorse any attorney, and we have not evaluated your legal matter. Submitting
  this form does not create an attorney-client relationship."
- Consent: "I agree that {platform} may share my inquiry with up to {N} participating
  attorneys, and that those attorneys may contact me by phone, text or email about my
  inquiry, including by automated means. Consent is not a condition of any purchase.
  Message and data rates may apply."
