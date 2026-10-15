"""Sends email. Locally that's Mailpit (a fake inbox you can open in a browser)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def send(self, payload: dict[str, Any]) -> None:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from
        msg["To"] = payload["to"]
        msg["Subject"] = payload["subject"]
        msg.set_content(payload["body"])
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=5) as smtp:
            if self.s.smtp_starttls:
                smtp.starttls()
            if self.s.smtp_username:
                smtp.login(self.s.smtp_username, self.s.smtp_password or "")
            smtp.send_message(msg)


class LogMailer:
    """Used when no SMTP server is configured: write the email to the log instead."""

    def send(self, payload: dict[str, Any]) -> None:
        log.info(
            "email (not sent, no SMTP configured) to=%s subject=%s",
            payload["to"],
            payload["subject"],
        )


def make_mailer(settings: Settings) -> SmtpMailer | LogMailer:
    return SmtpMailer(settings) if settings.smtp_host else LogMailer()
