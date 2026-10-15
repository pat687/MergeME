"""The words in every email the system sends. Plain text, short and specific."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.domain import compliance
from app.domain.types import BillingPlan, PracticeArea
from app.models import Attorney, Lead, Offer


def _money(cents: int) -> str:
    return "included in your plan" if cents == 0 else f"${cents / 100:,.2f}"


def offer_email(
    settings: Settings, lead: Lead, offer: Offer, attorney: Attorney, link: str
) -> dict[str, Any]:
    area = PracticeArea(lead.practice_area).label
    expires = offer.expires_at.strftime("%H:%M UTC") if offer.expires_at else "no deadline"
    flags = "\n".join(f"  - {f}" for f in lead.flags) or "  (none)"
    how_priced = f"\n                {offer.price_note}" if offer.price_note else ""
    bids_line = (
        ""
        if attorney.plan == BillingPlan.SUBSCRIPTION
        else "\nTo see where you rank or change your bids, use the private bid link we sent\n"
        "you, or ask us for a new one.\n"
    )
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
{bids_line}
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


def _privacy_footer(settings: Settings) -> str:
    base = settings.public_base_url.rstrip("/")
    who = (
        f"{settings.platform_name} ({settings.operator})"
        if settings.operator
        else (settings.platform_name)
    )
    return (
        f"{who}. Privacy policy: {base}/privacy. To ask what we hold about you, or to have it "
        f"deleted, email {settings.privacy_email} with your reference number."
    )


def _masked(lead: Lead) -> str:
    """The consent as agreed, with all but the last 4 digits of the number hidden: the
    address it goes to was typed in and hasn't been checked."""
    text = lead.consent_text or ""
    if not lead.phone or len(lead.phone) != 10:
        return text
    return text.replace(compliance.phone_display(lead.phone), f"(***) ***-{lead.phone[-4:]}")


def consumer_receipt(settings: Settings, lead: Lead) -> dict[str, Any]:
    # No user-supplied text in this email: it goes to whatever address was typed in,
    # so it must never be usable to send someone else a message from our domain.
    body = f"""Hello,

{compliance.consumer_message(lead.status, settings.platform_name)}

Reference number: {lead.id[:8].upper()}

What you agreed to (form version {lead.consent_version}):
"{_masked(lead)}"

{compliance.disclosure(settings.platform_name, settings.operator)}

{_privacy_footer(settings)}
"""
    return {
        "to": lead.email,
        "subject": f"We received your inquiry - {settings.platform_name}",
        "body": body,
    }


def consumer_assigned(settings: Settings, lead: Lead, attorney: Attorney) -> dict[str, Any]:
    """Tells the person who now has their details. Still no text they typed: only our
    wording and the attorney's details from our own records."""
    phone = f"\n  Phone:    {attorney.phone}" if attorney.phone else ""
    body = f"""Hello,

Your inquiry (reference {lead.id[:8].upper()}) was accepted by a participating attorney,
who now has your name, contact details and description and may contact you:

  Attorney: {attorney.name}
  Firm:     {attorney.firm}{phone}
  Email:    {attorney.email}

You don't have to hire them, and {settings.platform_name} does not recommend or vouch for
any attorney. You can check an attorney's license with your state bar.

To stop contact from this attorney, tell them, or reply STOP to their text messages.

{_privacy_footer(settings)}
"""
    return {
        "to": lead.email,
        "subject": f"An attorney received your inquiry - {settings.platform_name}",
        "body": body,
    }


def deletion_request(settings: Settings, lead_ref: str, attorney: Attorney) -> dict[str, Any]:
    """Sent to each attorney who received a lead when the person asks us to delete it."""
    body = f"""Hello {attorney.name},

The person behind inquiry {lead_ref}, which you received through {settings.platform_name},
has asked us to delete their information. We have deleted it from our systems except
what the law requires us to keep.

Under your participation agreement, please delete it from your systems too, unless you
now represent this person or must keep it for another legal reason.

{_privacy_footer(settings)}
"""
    return {
        "to": attorney.email,
        "subject": f"Deletion request for inquiry {lead_ref}",
        "body": body,
    }


def bids_email(settings: Settings, attorney: Attorney, link: str) -> dict[str, Any]:
    if attorney.plan == BillingPlan.SUBSCRIPTION:
        what = """On that page you can see your practice areas and what leads have sold for in
the last 30 days. Your leads are included in your subscription, so you don't
bid; contact us to change your practice areas."""
        keep = "Keep this link private."
    else:
        what = """On that page you can see, for each practice area, your place in line, a bid
that would put you first, and what leads have sold for in the last 30 days,
and change your bids. Changes apply to the next lead.

You only pay for leads you accept, and the price never depends on whether you
are hired."""
        keep = "Keep this link private: anyone with it can change your bids."
    body = f"""Hello {attorney.name},

Here is your private link to your lead bids page with {settings.platform_name}:

  {link}

{what}

{keep} This link replaces any earlier one; ask us for a new link at any time
and this one stops working.
"""
    return {"to": attorney.email, "subject": "Manage your lead bids", "body": body}
