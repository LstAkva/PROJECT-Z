"""
Google Sign-In (Google Identity Services ID token -> POST /api/auth/google).

Covers the shared auth modal integration (homepage, Daily), the CSP needed by Google's button,
and account linking: linking Google to an existing password account must keep it a password
account (previously it was switched to "google", which blocked changing the password).
"""
import uuid
from unittest.mock import patch

import pytest

CLIENT_ID = "mock-client-id.apps.googleusercontent.com"


def _google_login(client, email, sub, verified=True):
    info = {"sub": sub, "email": email, "name": "Google User", "email_verified": verified}
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=info):
        return client.post("/api/auth/google", json={"credential": "mock-jwt-token"})


@pytest.fixture
def google_enabled(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)


def test_config_exposes_client_id_only_when_set(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert client.get("/api/auth/config").json()["google_client_id"] == ""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    assert client.get("/api/auth/config").json()["google_client_id"] == CLIENT_ID


def test_google_endpoint_refuses_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    res = client.post("/api/auth/google", json={"credential": "x"})
    assert res.status_code >= 400


def test_new_google_user_is_google_account_and_not_duplicated(client, google_enabled):
    email, sub = f"g_{uuid.uuid4().hex[:6]}@gmail.com", f"sub-{uuid.uuid4().hex[:8]}"
    first = _google_login(client, email, sub)
    assert first.status_code == 200
    assert first.json()["user"]["auth_provider"] == "google"
    second = _google_login(client, email, sub)
    assert second.status_code == 200
    assert second.json()["user"]["id"] == first.json()["user"]["id"]


def test_linking_google_keeps_existing_password_account_usable(client, google_enabled):
    """The client fixture registered testplayer@zakowhat.uz with a password."""
    res = _google_login(client, "testplayer@zakowhat.uz", f"sub-{uuid.uuid4().hex[:8]}")
    assert res.status_code == 200
    assert res.json()["user"]["auth_provider"] == "local"

    change = client.post("/api/auth/change-password",
                         json={"current_password": "TestPassword123!", "new_password": "NewPassword456!"})
    assert change.status_code == 200, change.text

    client.post("/api/auth/logout")
    login = client.post("/api/auth/login", json={"email": "testplayer@zakowhat.uz", "password": "NewPassword456!"})
    assert login.status_code == 200


def test_unverified_google_email_is_rejected(client, google_enabled):
    res = _google_login(client, f"u_{uuid.uuid4().hex[:6]}@gmail.com", "sub-x", verified=False)
    assert res.status_code == 401


def test_csp_allows_google_button(client):
    csp = client.get("/").headers["content-security-policy"]
    for directive in ("script-src", "style-src", "frame-src", "connect-src"):
        part = next(p for p in csp.split(";") if p.strip().startswith(directive))
        assert "https://accounts.google.com" in part, directive


@pytest.mark.parametrize("page", ["/", "/daily"])
def test_shared_auth_modal_offers_google_lazily(client, page):
    html = client.get(page).text
    assert 'id="auth-google-block"' in html and 'id="auth-google-container"' in html
    assert "/api/auth/google" in html
    # Google's script is not loaded for every visitor; only on first opening of the login modal.
    assert '<script src="https://accounts.google.com/gsi/client"' not in html
    assert "script.src = 'https://accounts.google.com/gsi/client'" in html
