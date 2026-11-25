"""
Outgoing email (standard library only — no provider SDK, no new dependency). Two ways to send, both set by
environment variables:

1. Brevo HTTPS API (used when BREVO_API_KEY is set). Needed on hosts that block outgoing SMTP ports, such as
   Railway below the Pro plan, which blocks ports 25/465/587/2525:

    BREVO_API_KEY   API key from Brevo (Settings → SMTP & API → API keys)
    MAIL_FROM       sender, e.g. "ZakoWhat <noreply@zakowhat.uz>"; must be a sender verified in Brevo
                    (falls back to SMTP_FROM)

2. Plain SMTP (any provider: Brevo, Resend, Mailgun, Gmail with an app password, ...):

    SMTP_HOST       e.g. smtp-relay.brevo.com            (required to send)
    SMTP_PORT       587 (STARTTLS, default) or 465 (SSL)
    SMTP_USERNAME   provider login
    SMTP_PASSWORD   provider password / API key
    SMTP_FROM       sender, e.g. "ZakoWhat <no-reply@zakowhat.uz>"   (required to send)

Without either configuration nothing is sent: send_email() logs a warning and returns False, and the
callers keep answering users exactly as before (no error is shown, nothing is revealed).
In the test environment messages are never sent; they are appended to OUTBOX for assertions.
"""
import json
import logging
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Dict, List, Optional

logger = logging.getLogger("zakowhat.mailer")

OUTBOX: List[Dict[str, str]] = []          # test environment only
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _is_test_environment() -> bool:
    return os.getenv("ENVIRONMENT") == "test" or os.getenv("TESTING") == "true"


def _sender() -> str:
    return (os.getenv("MAIL_FROM", "").strip() or os.getenv("SMTP_FROM", "").strip())


def _api_configured() -> bool:
    return bool(os.getenv("BREVO_API_KEY", "").strip() and _sender())


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def mail_configured() -> bool:
    return _api_configured() or _smtp_configured()


def _send_via_brevo_api(to: str, subject: str, text: str, html: Optional[str]) -> bool:
    name, address = parseaddr(_sender())
    sender = {"email": address}
    if name:
        sender["name"] = name
    body = {"sender": sender, "to": [{"email": to}], "subject": subject, "textContent": text}
    if html:
        body["htmlContent"] = html
    request = urllib.request.Request(
        BREVO_API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"api-key": os.getenv("BREVO_API_KEY", "").strip(), "content-type": "application/json",
                 "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if 200 <= response.status < 300:
                return True
            logger.error("Email sending failed: Brevo API HTTP %s", response.status)
    except urllib.error.HTTPError as exc:
        # Brevo's error body names the problem (e.g. unauthorized, sender not valid); it never contains the key
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("code", "")
        except Exception:
            pass
        logger.error("Email sending failed: Brevo API HTTP %s %s", exc.code, detail)
    except Exception as exc:
        logger.error("Email sending failed: Brevo API %s: %s", exc.__class__.__name__, exc)
    return False


def _send_via_smtp(to: str, subject: str, text: str, html: Optional[str]) -> bool:
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
        # The reason (e.g. "Network is unreachable", "Username and Password not accepted") never contains
        # the password; it says whether the host blocks SMTP or the login is wrong.
        logger.error("Email sending failed: SMTP %s: %s", exc.__class__.__name__, exc)
        return False


def send_email(to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
    """Sends one message. Returns True when handed to the provider (or captured in tests)."""
    if _is_test_environment():
        OUTBOX.append({"to": to, "subject": subject, "text": text, "html": html or ""})
        return True
    if _api_configured():
        return _send_via_brevo_api(to, subject, text, html)
    if _smtp_configured():
        return _send_via_smtp(to, subject, text, html)
    logger.warning("Email not sent (no BREVO_API_KEY or SMTP_HOST / sender configured): subject=%r", subject)
    return False
