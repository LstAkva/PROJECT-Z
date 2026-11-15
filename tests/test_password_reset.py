"""
Password reset: signed one-time links, no account enumeration, no schema change.
Mail is captured in services.mailer.OUTBOX in the test environment.
"""
import re
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import status

from api.auth import (ALGORITHM, PASSWORD_RESET_GENERIC_MESSAGE, create_access_token, create_password_reset_token,
                      get_secret_key, hash_password, verify_password)
from models import User
from services import mailer


@pytest.fixture(autouse=True)
def clean_outbox():
    mailer.OUTBOX.clear()
    yield
    mailer.OUTBOX.clear()


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


def make_user(db, email, name="Ali Valiyev", username=None, provider="local", password="EskiParol1"):
    user = User(email=email, display_name=name, username=username, auth_provider=provider,
                hashed_password=hash_password(password) if password else None)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def request_reset(client, email):
    res = client.post("/api/auth/password-reset/request", json={"email": email})
    assert res.status_code == status.HTTP_200_OK, res.text
    return res.json()


def token_from_mail():
    assert len(mailer.OUTBOX) == 1
    m = re.search(r"/reset-password\?token=([A-Za-z0-9_\-\.]+)", mailer.OUTBOX[0]["text"])
    assert m, mailer.OUTBOX[0]["text"]
    return m.group(1)


def confirm(client, token, password):
    return client.post("/api/auth/password-reset/confirm", json={"token": token, "new_password": password})


def login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_full_reset_flow(anon, db_session):
    make_user(db_session, "ali@example.com")
    body = request_reset(anon, "Ali@Example.com")                      # case-insensitive
    assert body["message"] == PASSWORD_RESET_GENERIC_MESSAGE
    mail = mailer.OUTBOX[0]
    assert mail["to"] == "ali@example.com" and "parolni tiklash" in mail["subject"].lower()
    assert "https://zakowhat.uz/reset-password?token=" in mail["text"]
    token = token_from_mail()
    res = confirm(anon, token, "YangiParol2")
    assert res.status_code == 200, res.text
    assert login(anon, "ali@example.com", "YangiParol2").status_code == 200
    assert login(anon, "ali@example.com", "EskiParol1").status_code == 401


def test_link_works_only_once(anon, db_session):
    make_user(db_session, "once@example.com")
    request_reset(anon, "once@example.com")
    token = token_from_mail()
    assert confirm(anon, token, "Birinchi1").status_code == 200
    second = confirm(anon, token, "Ikkinchi2")
    assert second.status_code == 400 and "yaroqsiz" in second.json()["detail"]


def test_older_links_die_when_the_password_changes(anon, db_session):
    user = make_user(db_session, "old@example.com")
    old_token = create_password_reset_token(user)
    user.hashed_password = hash_password("BoshqaParol9")             # e.g. changed in settings
    db_session.commit()
    assert confirm(anon, old_token, "Yangi12345").status_code == 400


def test_expired_and_forged_tokens_are_rejected(anon, db_session):
    user = make_user(db_session, "exp@example.com")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    expired = jwt.encode({"sub": str(user.id), "purpose": "password_reset", "fp": "x", "exp": past},
                         get_secret_key(), algorithm=ALGORITHM)
    assert confirm(anon, expired, "Yangi12345").status_code == 400
    forged = jwt.encode({"sub": str(user.id), "purpose": "password_reset", "fp": "x",
                         "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, "not-the-secret", algorithm=ALGORITHM)
    assert confirm(anon, forged, "Yangi12345").status_code == 400
    # a normal sign-in token can never reset a password
    assert confirm(anon, create_access_token(user.id, user.email), "Yangi12345").status_code == 400
    db_session.refresh(user)
    assert verify_password("EskiParol1", user.hashed_password)


def test_same_answer_whether_or_not_the_account_exists(anon, db_session):
    make_user(db_session, "real@example.com")
    a = request_reset(anon, "real@example.com")
    b = request_reset(anon, "nobody@example.com")
    assert a == b and len(mailer.OUTBOX) == 1


def test_no_mail_to_generated_addresses_or_google_accounts(anon, db_session):
    # registration without an email stores {username}@zakowhat.uz — nobody reads that inbox
    make_user(db_session, "temur07@zakowhat.uz", name="temur07", username="temur07")
    make_user(db_session, "g@example.com", provider="google", password=None)
    assert request_reset(anon, "temur07@zakowhat.uz")["message"] == PASSWORD_RESET_GENERIC_MESSAGE
    assert request_reset(anon, "g@example.com")["message"] == PASSWORD_RESET_GENERIC_MESSAGE
    assert mailer.OUTBOX == []
    # a real mailbox on the domain still gets mail
    make_user(db_session, "support@zakowhat.uz", name="Support Team")
    request_reset(anon, "support@zakowhat.uz")
    assert len(mailer.OUTBOX) == 1


def test_weak_new_password_rejected(anon, db_session):
    user = make_user(db_session, "weak@example.com")
    assert confirm(anon, create_password_reset_token(user), "123").status_code == 422


def test_reset_page_is_private_and_uncached(anon):
    res = anon.get("/reset-password")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store" and res.headers["referrer-policy"] == "no-referrer"
    assert '<meta name="robots" content="noindex, nofollow">' in res.text
    assert "Disallow: /reset-password" in anon.get("/robots.txt").text


def test_sign_in_window_links_to_reset(anon):
    html = anon.get("/").text
    assert 'href="/reset-password"' in html and "Parolni unutdingizmi?" in html


def test_mailer_does_nothing_without_configuration(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    assert mailer.send_email("x@example.com", "s", "t") is False and mailer.OUTBOX == []
