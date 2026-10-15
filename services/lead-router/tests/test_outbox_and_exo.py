import json
from datetime import timedelta

import httpx

from app.config import Settings
from app.db import utcnow
from app.models import OutboxMessage
from app.services import outbox
from app.services.exo_sync import ExoLeadCaptureClient, ExoSyncError, lead_payload
from tests.conftest import inquiry_data


def test_failed_delivery_is_retried_with_backoff_then_parked(app, db):
    outbox.enqueue(db, "email", {"to": "x@example.com", "subject": "s", "body": "b"})
    db.commit()

    def broken(_payload):
        raise ConnectionError("smtp down")

    now = utcnow()
    for attempt in range(1, 4):
        delivered = outbox.deliver_due(
            app.state.session_factory, {"email": broken}, now=now, max_attempts=3
        )
        assert delivered == 0
        db.expire_all()
        msg = db.query(OutboxMessage).one()
        assert msg.attempts == attempt
        now = msg.next_attempt_at  # jump to when the retry is due

    assert msg.failed is True
    assert "smtp down" in msg.last_error


def test_backoff_doubles_and_is_capped():
    assert [outbox.backoff(n).seconds for n in (1, 2, 3)] == [10, 20, 40]
    assert outbox.backoff(20) == timedelta(minutes=5)


def test_unknown_message_kind_is_not_silently_dropped(app, db):
    outbox.enqueue(db, "carrier.pigeon", {})
    db.commit()
    outbox.deliver_due(app.state.session_factory, {}, now=utcnow(), max_attempts=5)
    db.expire_all()
    assert "no handler" in db.query(OutboxMessage).one().last_error


# --------------------------------------------------------------------------- #
# eXo lead-capture client (HTTP calls are faked with httpx.MockTransport)
# --------------------------------------------------------------------------- #
def exo_settings() -> Settings:
    return Settings(
        _env_file=None,
        exo_sync_enabled=True,
        exo_base_url="http://exo.test",
        exo_leadcapture_token="capture-token",
        exo_username="router-bot",
        exo_password="pw",
        demo_mode=True,
    )


def test_create_lead_sends_token_and_form_payload(client):
    lead_id = client.post("/api/v1/intake", json=inquiry_data()).json()["lead_id"]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="lead synchronized")

    from app.models import Lead

    with client.app.state.session_factory() as db:
        lead = db.get(Lead, lead_id)
        payload, full = lead_payload(lead), lead_payload(lead, contact_details=True)
    ExoLeadCaptureClient(exo_settings(), httpx.MockTransport(handler)).create_lead(payload)

    req = seen[0]
    assert req.url.path == "/portal/rest/leadcapture/leadsmanagement/leads"
    assert req.headers["token"] == "capture-token"
    body = json.loads(req.content)
    fields = {f["name"]: f["value"] for f in body["response"]["fields"]}
    assert fields["practice_area"] == "personal_injury"
    assert fields["distribution_model"] == "ping_tree"
    # By default eXo gets nothing personal; the router stays the one copy to erase.
    assert body["lead"]["mail"] == f"{lead_id}@leads.invalid"
    assert "Rivera" not in req.content.decode() and "rear-ended" not in req.content.decode()
    assert full["lead"]["mail"] == "jamie.rivera@example.org"


def test_assign_looks_up_the_exo_id_then_patches(client):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "GET":
            return httpx.Response(200, json={"leads": [{"id": 42}], "size": 1})
        assert json.loads(request.content) == {"id": 42, "assignee": "sam.okafor"}
        return httpx.Response(200, text="lead assigned")

    exo = ExoLeadCaptureClient(exo_settings(), httpx.MockTransport(handler))
    exo.assign_lead({"email": "jamie.rivera@example.org", "assignee": "sam.okafor"})
    assert [m for m, _ in calls] == ["GET", "PATCH"]


def test_assign_before_create_raises_so_the_outbox_retries():
    exo = ExoLeadCaptureClient(
        exo_settings(), httpx.MockTransport(lambda r: httpx.Response(200, json={"leads": []}))
    )
    try:
        exo.assign_lead({"email": "nobody@example.org", "assignee": "x"})
    except ExoSyncError as err:
        assert "retry" in str(err)
    else:
        raise AssertionError("expected ExoSyncError")


# --------------------------------------------------------------------------- #
# Worker health check (used by Docker to spot a stuck worker)
# --------------------------------------------------------------------------- #
def test_worker_heartbeat_freshness(tmp_path):
    from app.worker import heartbeat_is_fresh

    beat = tmp_path / "heartbeat"
    assert not heartbeat_is_fresh(beat, 30)  # never ran yet
    beat.touch()
    mtime = beat.stat().st_mtime
    assert heartbeat_is_fresh(beat, 30, now=mtime + 29)
    assert not heartbeat_is_fresh(beat, 30, now=mtime + 31)


_SMTP_CONNECTIONS: list = []


class _FakeSmtp:
    """Records what the mailer does instead of talking to a real server."""

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        _SMTP_CONNECTIONS.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.sent.append(msg)


def _mailer(monkeypatch, **overrides):
    import smtplib

    from app.config import Settings
    from app.services.notifier import SmtpMailer

    plain, ssl = type("Plain", (_FakeSmtp,), {}), type("Ssl", (_FakeSmtp,), {})
    monkeypatch.setattr(smtplib, "SMTP", plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", ssl)
    _SMTP_CONNECTIONS.clear()
    settings = Settings(_env_file=None, demo_mode=True, smtp_host="smtp.hostinger.com", **overrides)
    SmtpMailer(settings).send({"to": "a@example.org", "subject": "Hi", "body": "Hello"})
    return _SMTP_CONNECTIONS[0]


def test_hostinger_port_465_uses_ssl_and_sends_from_info_with_the_platform_name(monkeypatch):
    smtp = _mailer(
        monkeypatch, smtp_port=465, smtp_ssl=True, smtp_username="info@legalleadrouter.com"
    )
    assert type(smtp).__name__ == "Ssl" and (smtp.host, smtp.port) == ("smtp.hostinger.com", 465)
    assert smtp.calls == [("login", "info@legalleadrouter.com")]  # no STARTTLS on SSL
    assert smtp.sent[0]["From"] == "Barrgo Legal Connect <info@legalleadrouter.com>"


def test_port_587_uses_starttls_and_keeps_an_explicit_display_name(monkeypatch):
    smtp = _mailer(
        monkeypatch,
        smtp_port=587,
        smtp_starttls=True,
        smtp_from="Legal Lead Router <info@legalleadrouter.com>",
    )
    assert type(smtp).__name__ == "Plain" and smtp.calls == ["starttls"]
    assert smtp.sent[0]["From"] == "Legal Lead Router <info@legalleadrouter.com>"
