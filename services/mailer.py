"""
Outgoing email over plain SMTP (standard library only — no provider SDK, no new dependency).

Configured entirely by environment variables, so any SMTP provider works (Brevo, Resend, Mailgun,
Gmail with an app password, ...):

    SMTP_HOST       e.g. smtp-relay.brevo.com            (required to send)
    SMTP_PORT       587 (STARTTLS, default) or 465 (SSL)
    SMTP_USERNAME   provider login
    SMTP_PASSWORD   provider password / API key
    SMTP_FROM       sender, e.g. "ZakoWhat <no-reply@zakowhat.uz>"   (required to send)

Without SMTP_HOST and SMTP_FROM nothing is sent: send_email() logs a warning and returns False, and the
callers keep answering users exactly as before (no error is shown, nothing is revealed).
In the test environment messages are never sent; they are appended to OUTBOX for assertions.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict, List, Optional

logger = logging.getLogger("zakowhat.mailer")

OUTBOX: List[Dict[str, str]] = []          # test environment only


def _is_test_environment() -> bool:
    return os.getenv("ENVIRONMENT") == "test" or os.getenv("TESTING") == "true"


def mail_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def send_email(to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
    """Sends one message. Returns True when handed to the SMTP server (or captured in tests)."""
    if _is_test_environment():
        OUTBOX.append({"to": to, "subject": subject, "text": text, "html": html or ""})
        return True
    if not mail_configured():
        logger.warning("Email not sent (SMTP_HOST / SMTP_FROM not configured): subject=%r", subject)
        return False

    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM").strip()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    host = os.getenv("SMTP_HOST").strip()
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls(context=context)
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        return True
    except Exception as exc:  # never let mail problems break a request; never log the message body
        logger.error("Email sending failed: %s", exc.__class__.__name__)
        return False
