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


def lead_payload(lead: Lead) -> dict[str, Any]:
    """Build the outbox payload for creating the lead in eXo."""
    fields = {
        "router_lead_id": lead.id,
        "practice_area": lead.practice_area,
        "zip_code": lead.zip_code,
        "state": lead.state,
        "score": str(lead.score),
        "tier": lead.tier or "",
        "distribution_model": lead.distribution_model or "",
        "description": lead.description,
    }
    return {
        "lead": {
            "mail": lead.email,
            "firstName": lead.first_name,
            "lastName": lead.last_name,
            "phone": lead.phone,
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
