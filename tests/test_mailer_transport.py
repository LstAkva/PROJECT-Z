"""
Mail transport choice: Brevo's HTTPS API when BREVO_API_KEY is set (hosts such as Railway Hobby block SMTP
ports), plain SMTP otherwise, and nothing sent (no crash) when neither is configured.
"""
import io
import json
import logging
import urllib.error
from unittest.mock import patch

import pytest

from services import mailer

MAIL_VARS = ("BREVO_API_KEY", "MAIL_FROM", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM")


@pytest.fixture
def live_env(monkeypatch):
    """Outside the test environment, with no mail settings at all."""
    monkeypatch.setenv("ENVIRONMENT", "production-like-test")
    monkeypatch.delenv("TESTING", raising=False)
    for name in MAIL_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class _Response:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_brevo_api_is_used_when_its_key_is_set(live_env):
    live_env.setenv("BREVO_API_KEY", "xkeysib-test")
    live_env.setenv("MAIL_FROM", "ZakoWhat <noreply@zakowhat.uz>")
    live_env.setenv("SMTP_HOST", "smtp.gmail.com")          # ignored while the API key is set
    live_env.setenv("SMTP_FROM", "x@gmail.com")
    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen, \
            patch("smtplib.SMTP") as smtp:
        assert mailer.send_email("p@misol.uz", "Mavzu", "Matn", "<p>Matn</p>") is True
    smtp.assert_not_called()
    request = urlopen.call_args[0][0]
    assert request.full_url == mailer.BREVO_API_URL and request.get_method() == "POST"
    assert request.get_header("Api-key") == "xkeysib-test"
    body = json.loads(request.data)
    assert body["sender"] == {"name": "ZakoWhat", "email": "noreply@zakowhat.uz"}
    assert body["to"] == [{"email": "p@misol.uz"}] and body["subject"] == "Mavzu"
    assert body["textContent"] == "Matn" and body["htmlContent"] == "<p>Matn</p>"


def test_brevo_sender_falls_back_to_smtp_from(live_env):
    live_env.setenv("BREVO_API_KEY", "xkeysib-test")
    live_env.setenv("SMTP_FROM", "team@misol.uz")
    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        assert mailer.send_email("p@misol.uz", "S", "T") is True
    assert json.loads(urlopen.call_args[0][0].data)["sender"] == {"email": "team@misol.uz"}


def test_brevo_error_is_logged_without_the_key(live_env, caplog):
    live_env.setenv("BREVO_API_KEY", "xkeysib-secret-value")
    live_env.setenv("MAIL_FROM", "noreply@zakowhat.uz")
    error = urllib.error.HTTPError(mailer.BREVO_API_URL, 401, "Unauthorized", {},
                                   io.BytesIO(b'{"code":"unauthorized","message":"Key not found"}'))
    with caplog.at_level(logging.ERROR, logger="zakowhat.mailer"), \
            patch("urllib.request.urlopen", side_effect=error):
        assert mailer.send_email("p@misol.uz", "S", "T") is False
    assert "401 unauthorized" in caplog.text and "xkeysib-secret-value" not in caplog.text


def test_smtp_failure_logs_the_reason_not_the_password(live_env, caplog):
    live_env.setenv("SMTP_HOST", "smtp.gmail.com")
    live_env.setenv("SMTP_FROM", "ZakoWhat <x@gmail.com>")
    live_env.setenv("SMTP_PASSWORD", "app-password-value")
    with caplog.at_level(logging.ERROR, logger="zakowhat.mailer"), \
            patch("smtplib.SMTP", side_effect=OSError(101, "Network is unreachable")):
        assert mailer.send_email("p@misol.uz", "S", "T") is False
    assert "Network is unreachable" in caplog.text and "app-password-value" not in caplog.text


def test_nothing_configured_sends_nothing(live_env):
    with patch("urllib.request.urlopen") as urlopen, patch("smtplib.SMTP") as smtp:
        assert mailer.send_email("p@misol.uz", "S", "T") is False
    urlopen.assert_not_called()
    smtp.assert_not_called()
    assert mailer.mail_configured() is False
