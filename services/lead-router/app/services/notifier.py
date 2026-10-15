"""Sends email. Locally that's Mailpit (a fake inbox you can open in a browser)."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        # Checks the server's certificate and name. Without it smtplib accepts any
        # certificate, and whoever intercepts the connection gets the mailbox password.
        self.tls = ssl.create_default_context()

    def sender(self) -> str:
        name, address = parseaddr(self.s.smtp_from)
        return formataddr((name or self.s.platform_name, address))

    def send(self, payload: dict[str, Any]) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender()
        msg["To"] = payload["to"]
        msg["Subject"] = payload["subject"]
        msg.set_content(payload["body"])
        host, port = self.s.smtp_host, self.s.smtp_port
        if self.s.smtp_ssl:
            connection = smtplib.SMTP_SSL(host, port, timeout=5, context=self.tls)
        else:
            connection = smtplib.SMTP(host, port, timeout=5)
        with connection as smtp:
            if self.s.smtp_starttls and not self.s.smtp_ssl:
                smtp.starttls(context=self.tls)
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
