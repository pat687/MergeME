# Compliance by design

> **Not legal advice.** This document explains how the software is built to support
> compliance. Attorney-advertising, lead-generation, telemarketing and privacy rules vary by
> state and change over time. Have a lawyer licensed in each state you operate in review the
> business model, the consumer-facing wording and your contracts before launch.

## 1. What kind of business this software assumes

Under the ABA Model Rules, a lawyer may pay for **advertising**, including paying a lead
generator, but may not pay someone for **recommending** them, and may not **share legal fees**
with a non-lawyer. A for-profit platform that isn't approved by a regulator as a
"qualified lawyer referral service" should therefore operate as **attorney advertising /
lead generation**, and must not present itself as a referral service that recommends lawyers.

The software is built around that model:

| Principle | Source |
|---|---|
| Lawyers may pay the reasonable cost of advertising, including lead generation | ABA Model Rule 7.2(b)(1) |
| A lead generator must not recommend the lawyer, imply the referral is free, or imply it analyzed the person's legal problem to choose the lawyer | ABA Model Rule 7.2, Comment [5]; Rule 7.1 |
| Payments must be consistent with the fee-division and independence rules | ABA Model Rules 1.5(e) and 5.4(a) |
| Lawyers may pay a *qualified* (regulator-approved) lawyer referral service | ABA Model Rule 7.2(b)(2) |
| Communications must not be false or misleading | ABA Model Rule 7.1 |

## 2. Rule → feature → proof

| Requirement | How the code meets it | Enforced in | Tested in |
|---|---|---|---|
| **No fee-splitting.** Never take a share of an attorney's fee | The ledger has four flat charge kinds (`per_lead`, `included`, `subscription`, `territory`). There is no percentage or outcome kind to choose. | [`domain/types.py`](../services/lead-router/app/domain/types.py) `ChargeKind` | `test_there_is_no_charge_kind_tied_to_case_outcomes` |
| Every charge is whole, non-negative cents | Single gate `assert_flat_fee` on every ledger insert | [`domain/compliance.py`](../services/lead-router/app/domain/compliance.py) | `test_ledger_gate_refuses_anything_but_flat_non_negative_cents` |
| Percentage pricing can't sneak in through the API | Attorney schemas use `extra="forbid"`: a `fee_percent` field is rejected with 422 | [`schemas.py`](../services/lead-router/app/schemas.py) `AttorneyBase` | `test_percentage_pricing_cannot_even_be_submitted` |
| **Price doesn't depend on case outcome** | Charges happen at delivery (accept, or exclusive assignment). There is no endpoint to report retention, settlement or recovery. | [`services/billing.py`](../services/lead-router/app/services/billing.py) | `test_ping_tree_walks_down_the_line...` (charge on accept) |
| **No recommending.** Don't imply we chose the "best" lawyer | Consumer text says attorneys *pay*, that we *do not recommend* any attorney, and that we *have not evaluated* the matter. | `compliance.disclosure()` | `test_consumer_wording_never_claims_to_recommend` |
| Don't imply the referral is free to the lawyer | Disclosure states attorneys pay a flat fee | `compliance.disclosure()` | same |
| Don't imply we analyzed their legal problem | The score is internal (pricing/ordering) and never shown to consumers. Routing uses the practice area the consumer chose plus objective facts (ZIP, license, capacity). | [`domain/scoring.py`](../services/lead-router/app/domain/scoring.py) docstring | n/a (by construction) |
| Not a law firm / no attorney-client relationship | Disclosure on the form, in the API response and in the receipt email | `compliance.disclosure()`, `emails.consumer_receipt` | `test_consumer_wording...` |
| "Attorney advertising" label | Header tag and first words of the disclosure | [`static/intake.html`](../services/lead-router/app/static/intake.html) | n/a |
| Attorney independence (Rule 5.4) | Attorneys decide to accept or pass on every lead; flags like "time-sensitive" are notes, never auto-rejections | `scoring._flags`, `offers.accept/decline` | `test_old_incident_gets_deadline_flag_but_is_not_rejected` |

## 3. Consumer consent and contact (TCPA and state telemarketing laws)

Attorneys will call and text consumers, so consent evidence matters.

| Control | Detail |
|---|---|
| Explicit opt-in | Unchecked checkbox. The server rejects any lead without `consent=true` (`scrubbing.scrub`). |
| Exact wording stored | `leads.consent_text` stores the full sentence the person agreed to, plus `consent_version`. |
| When and from where | `consent_at`, `ip_address`, `user_agent` are stored on the lead. |
| Bounded sharing | The consent names the maximum number of attorneys (`ROUTER_SHARED_MAX_ATTORNEYS`), and the router never offers to more at once. Every attorney who received the lead is in its audit trail. |
| Not a condition of purchase | Included in the consent text. |

Notes for counsel:
- The FCC's 2023 "one-to-one consent" rule for lead generators was vacated by the
  Eleventh Circuit in January 2025 (*Insurance Marketing Coalition v. FCC*). Storing
  per-lead consent and the list of recipients is still best practice, and it supports a
  one-to-one style flow if a state or a future rule requires it.
- Several states have their own telemarketing statutes (for example, Florida's FTSA) with
  stricter consent and calling-hour rules. Calling is done by attorneys, but your contracts
  should require compliance.

## 4. Privacy and confidentiality

| Concern | Control |
|---|---|
| Prospective-client information (Rule 1.18) | Offer previews and offer emails contain no name, phone or email, only practice area, ZIP, tier and a description summary. Attorneys who pass never learn who the person is. |
| Minimum necessary | The intake form asks people not to include passwords or account numbers. |
| Access control | Admin endpoints require `X-Admin-Key`; attorneys only see offers through signed links. |
| Data location | Everything runs on your own infrastructure (Postgres volume, eXo volumes). No third-party SaaS. |
| Selling personal information (e.g. CCPA/CPRA in California) | Selling leads may count as "selling" personal information. Add the required notice and opt-out if you serve those states. *Not implemented; see the launch checklist.* |

## 5. When nobody can help

If no participating attorney covers the request, the person is told so plainly and pointed
to their state or local bar association's lawyer referral service (`LeadStatus.UNSERVICEABLE`,
`compliance.consumer_message`). No dead ends, and no pretending.

## 6. Launch checklist (business and legal)

- [ ] Counsel review of the disclosure, the consent text and the attorney agreement in each state
- [ ] Confirm the business doesn't need state registration or certification. For example,
      California requires lawyer referral services to be certified by the State Bar; decide
      whether your model is advertising or referral, and name it accordingly.
- [ ] Attorney agreement: flat fees only, attorney decides on every matter, responsible for
      their own conflicts checks, advertising rules and calling-law compliance
- [ ] Some states require ads to name a responsible lawyer or firm, or to be filed with the
      bar. Check before running campaigns.
- [ ] Privacy policy, data-retention period and deletion process (and CCPA/CPRA "Do Not
      Sell or Share" handling if applicable)
- [ ] Replace the demo disclosure version `2026-10-01` with your reviewed version and keep
      old versions on file (leads store which version they agreed to)

## 7. Changing the wording safely

All consumer-facing compliance text lives in one file,
[`domain/compliance.py`](../services/lead-router/app/domain/compliance.py). To change it:

1. Edit the text and bump `ROUTER_DISCLOSURE_VERSION`.
2. Run `make test`. `test_consumer_wording_never_claims_to_recommend` guards against
   phrases like "best", "top-rated" or "evaluated your case".
3. Archive the old wording. Existing leads keep the exact text they agreed to.
