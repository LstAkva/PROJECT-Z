"""
Regressions:
1. The footer cookie banner called PATCH /api/auth/profile/settings, which does not exist (404),
   so a signed-in user's consent was never stored. It must use POST /api/auth/consent.
2. Login/register password fields get a show/hide toggle (static/js/zakowhat-navbar.js).
"""
import re
from pathlib import Path

import pytest

from main import static_url


def test_footer_saves_consent_through_existing_endpoint():
    footer = Path("templates/components/footer.html").read_text(encoding="utf-8")
    assert "/api/auth/profile/settings" not in footer
    assert "fetch('/api/auth/consent'" in footer
    assert "method: 'POST'" in footer
    assert "cookie_consent:" in footer


def test_consent_endpoint_stores_choice_for_signed_in_user(client, db_session):
    from models import User
    res = client.post("/api/auth/consent", json={"cookie_consent": "all"})
    assert res.status_code == 200
    user = db_session.query(User).filter_by(email="testplayer@zakowhat.uz").one()
    db_session.refresh(user)
    assert user.cookie_consent == "all"
    assert user.consent_recorded_at is not None


def test_no_page_calls_missing_profile_settings_endpoint(client):
    for page in ("/", "/arena", "/daily", "/privacy", "/terms"):
        assert "/api/auth/profile/settings" not in client.get(page).text


@pytest.mark.parametrize("page", ["/", "/arena", "/daily"])
def test_password_fields_are_marked_for_toggle(client, page):
    html = client.get(page).text
    marked = re.findall(r'<input type="password" data-password-toggle id="(login-password|register-password)"', html)
    assert sorted(set(marked)) == ["login-password", "register-password"]
    assert "js/zakowhat-navbar.js" in html


def test_navbar_script_has_accessible_toggle(client):
    body = client.get(static_url("js/zakowhat-navbar.js")).text
    assert "input[data-password-toggle]" in body
    assert "btn.type = 'button'" in body                 # never submits the form
    assert "aria-pressed" in body and "aria-label" in body
    assert "Parolni ko‘rsatish" in body and "Parolni yashirish" in body
    assert "setSelectionRange" in body                   # caret position is kept
