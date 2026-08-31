# app/mailer.py — thin SMTP helper for the local postfix mailserver
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from flask import current_app

log = logging.getLogger(__name__)


def send_email(*, to: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    """
    Send a simple email via the configured SMTP host.
    Raises on failure so callers can flash a useful message.
    """
    host = current_app.config.get("MAIL_SMTP_HOST") or "mail.supergalley.com"
    port = int(current_app.config.get("MAIL_SMTP_PORT") or 25)
    from_addr = current_app.config.get("MAIL_FROM") or "satan@bannedfromheaven.com"
    from_name = current_app.config.get("MAIL_FROM_NAME") or "BannedFromHeaven"
    # Domain part of Message-ID should match the From domain (deliverability).
    msgid_domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "bannedfromheaven.com"

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=msgid_domain)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo()
        # STARTTLS is optional; port 25 on the local server works without it.
        if smtp.has_extn("starttls"):
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                # Local postfix may not need TLS; plain is fine on private net.
                pass
        smtp.send_message(msg)

    log.info("Sent mail to %s subject=%r via %s:%s", to, subject, host, port)
