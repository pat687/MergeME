"""The words in every email the system sends. Plain text, short and specific."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.domain import compliance
from app.domain.types import PracticeArea
from app.models import Attorney, Lead, Offer
from app.services import links, market


def _money(cents: int) -> str:
    return "included in your plan" if cents == 0 else f"${cents / 100:,.2f}"


def offer_email(
    settings: Settings, lead: Lead, offer: Offer, attorney: Attorney, link: str
) -> dict[str, Any]:
    area = PracticeArea(lead.practice_area).label
    expires = offer.expires_at.strftime("%H:%M UTC") if offer.expires_at else "no deadline"
    flags = "\n".join(f"  - {f}" for f in lead.flags) or "  (none)"
    note = market.price_note(settings, lead.distribution_model, offer.price_cents)
    how_priced = f"\n                {note}" if note else ""
    bids = links.bids_link(settings.public_base_url, settings.link_signing_secret, attorney.id)
    body = f"""Hello {attorney.name},

A new {area} inquiry is available in {lead.zip_code}, {lead.state}.

  Quality tier: {lead.tier} (score {lead.score}/100)
  Price:        {_money(offer.price_cents)}{how_priced}
  Respond by:   {expires}

What they told us (contact details are shared once you accept):
  "{lead.summary()}"

Notes:
{flags}

Accept or decline here:
  {link}

See where you rank and change your bids any time:
  {bids}

You decide whether to take on any matter. {settings.platform_name} does not
evaluate cases or recommend attorneys to consumers.
"""
    return {"to": attorney.email, "subject": f"New {area} inquiry in {lead.zip_code}", "body": body}


def delivered_email(settings: Settings, lead: Lead, offer: Offer, attorney: Attorney) -> dict:
    area = PracticeArea(lead.practice_area).label
    incident = lead.incident_date.isoformat() if lead.incident_date else "not given"
    body = f"""Hello {attorney.name},

This {area} inquiry is now yours. Please reach out promptly.

  Name:     {lead.first_name} {lead.last_name}
  Phone:    ({lead.phone[:3]}) {lead.phone[3:6]}-{lead.phone[6:]}
  Email:    {lead.email}
  Location: {lead.zip_code}, {lead.state}
  Incident: {incident}

In their words:
  "{lead.description}"

Consent: the consumer agreed to be contacted (form version {lead.consent_version},
{lead.consent_at:%Y-%m-%d %H:%M} UTC). Charge: {_money(offer.price_cents)}.
"""
    return {
        "to": attorney.email,
        "subject": f"Lead delivered: {area} in {lead.zip_code}",
        "body": body,
    }


def consumer_receipt(settings: Settings, lead: Lead) -> dict[str, Any]:
    # No user-supplied text in this email: it goes to whatever address was typed in,
    # so it must never be usable to send someone else a message from our domain.
    body = f"""Hello,

{compliance.consumer_message(lead.status, settings.platform_name)}

Reference number: {lead.id[:8].upper()}

{compliance.disclosure(settings.platform_name)}
"""
    return {
        "to": lead.email,
        "subject": f"We received your inquiry - {settings.platform_name}",
        "body": body,
    }


def bids_email(settings: Settings, attorney: Attorney, link: str) -> dict[str, Any]:
    body = f"""Hello {attorney.name},

Here is your private link to manage your lead bids with {settings.platform_name}:

  {link}

On that page you can see, for each practice area, where your bid ranks, the bid
that would put you first in line, and what leads have sold for in the last 30
days, and change your bids. Changes apply to the next lead.

You only pay for leads you accept, and the price never depends on whether you
are hired. Keep this link private: anyone with it can change your bids.
"""
    return {"to": attorney.email, "subject": "Manage your lead bids", "body": body}
