"""Mirror leads into eXo Platform's "lead-capture" add-on.

The router is the source of truth; eXo is where people *look* at the pipeline
alongside their documents and tasks. If eXo is down, routing keeps working and
the outbox retries the sync later.

Endpoints and payload shapes come from the add-on's source code
(github.com/exo-addons/lead-capture, LeadsManagementRest.java):

* POST  /portal/rest/leadcapture/leadsmanagement/leads   header "token" = capture token
* GET   /portal/rest/leadcapture/leadsmanagement/leads?search=<email>   (basic auth)
* PATCH /portal/rest/leadcapture/leadsmanagement/assign  {"id", "assignee"}  (basic auth)
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.models import Lead

API = "/portal/rest/leadcapture/leadsmanagement"


class ExoSyncError(RuntimeError):
    pass


def exo_email(lead: Lead, contact_details: bool) -> str:
    """The address eXo knows the lead by. Without contact details it's a placeholder on the
    reserved .invalid domain, so it can never reach anyone."""
    return lead.email if contact_details else f"{lead.id}@leads.invalid"


def lead_payload(lead: Lead, contact_details: bool = False) -> dict[str, Any]:
    """Build the outbox payload for creating the lead in eXo. Without contact_details
    (ROUTER_EXO_SYNC_CONTACT_DETAILS) eXo gets no name, email, phone or description, so
    erasing a lead in the router (services/privacy.py) leaves nothing personal in eXo."""
    ref = lead.id[:8].upper()
    fields = {
        "router_lead_id": lead.id,
        "practice_area": lead.practice_area,
        "zip_code": lead.zip_code,
        "state": lead.state,
        "score": str(lead.score),
        "tier": lead.tier or "",
        "distribution_model": lead.distribution_model or "",
        "description": lead.description if contact_details else "(kept in the lead router)",
    }
    return {
        "lead": {
            "mail": exo_email(lead, contact_details),
            "firstName": lead.first_name if contact_details else "Lead",
            "lastName": lead.last_name if contact_details else ref,
            "phone": lead.phone if contact_details else "",
            "inferredCountry": "US",
            "personSource": "Barrgo intake",
            "captureSourceInfo": f"router lead {lead.id}",
            "landingPageInfo": lead.utm_source or "",
        },
        "response": {
            "formName": "Barrgo legal intake",
            "fields": [{"name": k, "value": v} for k, v in fields.items()],
        },
    }


class ExoLeadCaptureClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        auth = None
        if settings.exo_username:
            auth = httpx.BasicAuth(settings.exo_username, settings.exo_password or "")
        self.token = settings.exo_leadcapture_token or ""
        self.http = httpx.Client(
            base_url=settings.exo_base_url,
            auth=auth,
            # Short timeouts: a hung eXo should cost the worker seconds, not minutes.
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
        )

    def create_lead(self, payload: dict[str, Any]) -> None:
        r = self.http.post(f"{API}/leads", json=payload, headers={"token": self.token})
        if r.status_code >= 300:
            raise ExoSyncError(f"eXo create failed: HTTP {r.status_code} {r.text[:200]}")

    def assign_lead(self, payload: dict[str, Any]) -> None:
        """payload = {"email": ..., "assignee": <eXo username>}"""
        r = self.http.get(
            f"{API}/leads", params={"search": payload["email"], "page": 0, "limit": 1}
        )
        if r.status_code >= 300:
            raise ExoSyncError(f"eXo lookup failed: HTTP {r.status_code}")
        leads = r.json().get("leads") or []
        if not leads:
            raise ExoSyncError("lead not in eXo yet; will retry after it is created")
        r = self.http.patch(
            f"{API}/assign", json={"id": leads[0]["id"], "assignee": payload["assignee"]}
        )
        if r.status_code >= 300:
            raise ExoSyncError(f"eXo assign failed: HTTP {r.status_code} {r.text[:200]}")
