"""
Gaps closed on 27 Nov 2026:
1. a name-only account can add an email (saved only after the link in the message is opened) and so recover
   its password; any account (except Google-only) can change its email the same way;
2. /health answers 503 when the database is down, so an uptime monitor notices;
3. rate limits fit a lecture hall on one Wi-Fi (one public IP), and rooms read the pack once, not per poll.
"""
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

import api.auth as auth
from models import User
from services import mailer
from tests.test_player_progress import _hdr, _user


@pytest.fixture(autouse=True)
def clean_outbox():
    mailer.OUTBOX.clear()
    yield
    mailer.OUTBOX.clear()


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


def _link_token(mail):
    m = re.search(r"/verify-email\?token=([A-Za-z0-9_\-\.]+)", mail["text"])
    assert m, mail["text"]
    return m.group(1)


# --- 1. add / change email --------------------------------------------------------------------------------------

def test_name_only_account_adds_an_email_and_can_then_reset_its_password(anon, db_session):
    user = _user(db_session, "sardor@zakowhat.uz", "Sardor")
    profile = anon.get("/api/auth/profile", headers=_hdr(user)).json()["user"]
    assert profile["email_is_placeholder"] is True and profile["email_managed_by_google"] is False

    res = anon.post("/api/auth/email/request", json={"email": "Sardor.Real@Misol.uz "}, headers=_hdr(user))
    assert res.status_code == 200 and "sardor.real@misol.uz" in res.json()["message"]
    assert len(mailer.OUTBOX) == 1 and mailer.OUTBOX[0]["to"] == "sardor.real@misol.uz"
    db_session.refresh(user)
    assert user.email == "sardor@zakowhat.uz"                     # nothing changes before the link is opened

    token = _link_token(mailer.OUTBOX[0])
    res = anon.post("/api/auth/email/confirm", json={"token": token})          # any browser, not signed in
    assert res.status_code == 200 and res.json()["email"] == "sardor.real@misol.uz"
    db_session.refresh(user)
    assert user.email == "sardor.real@misol.uz"
    assert anon.post("/api/auth/email/confirm", json={"token": token}).status_code == 400   # works once
    assert anon.get("/api/auth/profile", headers=_hdr(user)).json()["user"]["email_is_placeholder"] is False

    mailer.OUTBOX.clear()                                           # password recovery now reaches the person
    assert anon.post("/api/auth/password-reset/request", json={"email": "sardor.real@misol.uz"}).status_code == 200
    assert len(mailer.OUTBOX) == 1


def test_still_signed_in_after_the_email_changes(anon, db_session):
    user = _user(db_session, "old@misol.uz", "Eski")
    headers = _hdr(user)
    anon.post("/api/auth/email/request", json={"email": "new@misol.uz"}, headers=headers)
    anon.post("/api/auth/email/confirm", json={"token": _link_token(mailer.OUTBOX[0])})
    assert anon.get("/api/auth/me", headers=headers).json()["user"]["email"] == "new@misol.uz"


@pytest.mark.parametrize("email,code", [("not-an-email", 422), ("someone@zakowhat.uz", 422), ("taken@misol.uz", 409),
                                         ("me@misol.uz", 400)])
def test_bad_new_emails_are_refused_and_nothing_is_sent(anon, db_session, email, code):
    _user(db_session, "taken@misol.uz", "Band")
    user = _user(db_session, "me@misol.uz", "Men")
    assert anon.post("/api/auth/email/request", json={"email": email}, headers=_hdr(user)).status_code == code
    assert mailer.OUTBOX == []


def test_email_taken_between_request_and_confirm(anon, db_session):
    user = _user(db_session, "a@misol.uz", "A")
    anon.post("/api/auth/email/request", json={"email": "race@misol.uz"}, headers=_hdr(user))
    _user(db_session, "race@misol.uz", "Tezkor")
    res = anon.post("/api/auth/email/confirm", json={"token": _link_token(mailer.OUTBOX[0])})
    assert res.status_code == 409
    db_session.refresh(user)
    assert user.email == "a@misol.uz"


def test_link_tokens_cannot_be_forged_or_swapped(anon, db_session):
    user = _user(db_session, "victim@misol.uz", "Qurbon")
    sign_in = auth.create_access_token(user.id, user.email)
    reset = auth.create_password_reset_token(user)
    for token in ("x" * 20, sign_in, reset):
        assert anon.post("/api/auth/email/confirm", json={"token": token}).status_code == 400
    db_session.refresh(user)
    assert user.email == "victim@misol.uz"


def test_google_only_accounts_manage_email_at_google(anon, db_session):
    user = User(email="g@gmail.com", display_name="Google", auth_provider="google", google_id="g-1", hashed_password=None)
    db_session.add(user)
    db_session.commit()
    assert anon.get("/api/auth/profile", headers=_hdr(user)).json()["user"]["email_managed_by_google"] is True
    assert anon.post("/api/auth/email/request", json={"email": "x@misol.uz"}, headers=_hdr(user)).status_code == 400


def test_signed_out_cannot_request(anon):
    assert anon.post("/api/auth/email/request", json={"email": "x@misol.uz"}).status_code == 401


def test_verify_page_and_settings_block(anon, db_session):
    page = anon.get("/verify-email")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer" and "history.replaceState" in page.text
    assert "/verify-email" in anon.get("/robots.txt").text
    arena = anon.get("/profile").text
    assert 'id="settings-email-section"' in arena and "/auth/email/request" in arena


# --- 2. health -----------------------------------------------------------------------------------------------------

def test_health_is_503_when_the_database_is_down(anon, monkeypatch):
    import main
    assert anon.get("/health").status_code == 200
    monkeypatch.setattr(main, "test_database_connection", lambda: False)
    res = anon.get("/health")
    assert res.status_code == 503 and res.json() == {"status": "degraded", "database": "unavailable"}
    assert res.headers["cache-control"] == "no-store"


# --- 3. a hall on one Wi-Fi ----------------------------------------------------------------------------------------

def _request(ip="203.0.113.7"):
    return SimpleNamespace(headers={"X-Forwarded-For": ip}, client=SimpleNamespace(host=ip))


@pytest.fixture
def live_limits(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("TESTING", raising=False)
    auth._RATE_LIMIT_BUCKETS.clear()
    yield
    auth._RATE_LIMIT_BUCKETS.clear()


def test_one_persons_password_guessing_does_not_lock_the_hall(live_limits):
    for _ in range(10):
        auth.enforce_rate_limit(_request(), "login_account", 10, 60, subject="victim")
    with pytest.raises(HTTPException) as e:
        auth.enforce_rate_limit(_request(), "login_account", 10, 60, subject="victim")
    assert e.value.status_code == 429
    for n in range(40):                                              # 40 classmates on the same Wi-Fi
        auth.enforce_rate_limit(_request(), "login_account", 10, 60, subject=f"student{n}")


def test_hall_sized_limits(live_limits):
    from api.rooms import MAX_TEAMS
    for _ in range(40):
        auth.enforce_rate_limit(_request(), "register", 40, 60)        # a class signs up at once
    for _ in range(MAX_TEAMS):
        auth.enforce_rate_limit(_request(), "room_join", MAX_TEAMS * 3, 60)


def test_room_answer_limit_is_per_team(anon, db_session, monkeypatch):
    from tests.test_player_progress import make_classic_pack
    monkeypatch.setenv("ROOMS_PLAYER_JOIN", "1")
    monkeypatch.setenv("OWNER_EMAIL", "hall-owner@zakowhat.uz")
    owner = _user(db_session, "hall-owner@zakowhat.uz", "Boshlovchi")
    pack = make_classic_pack(db_session, "Zal")
    code = anon.post("/api/rooms", json={"quiz_id": pack.id}, headers=_hdr(owner)).json()["code"]
    tokens = [anon.post("/api/rooms/join", json={"code": code, "team_name": f"Jamoa {n}"}).json()["token"] for n in range(3)]
    anon.post(f"/api/rooms/{code}/start", headers=_hdr(owner))
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("TESTING", raising=False)
    auth._RATE_LIMIT_BUCKETS.clear()
    for _ in range(30):                                                # team 0 exhausts its own budget
        anon.post(f"/api/rooms/{code}/answer", json={"text": "x"}, headers={"X-Team-Token": tokens[0]})
    assert anon.post(f"/api/rooms/{code}/answer", json={"text": "x"}, headers={"X-Team-Token": tokens[0]}).status_code == 429
    assert anon.post(f"/api/rooms/{code}/answer", json={"text": "Javob 1"}, headers={"X-Team-Token": tokens[1]}).status_code == 200
    auth._RATE_LIMIT_BUCKETS.clear()


def test_room_polls_do_not_reload_the_pack(anon, db_session, monkeypatch):
    from api import rooms
    from tests.test_player_progress import make_classic_pack
    monkeypatch.setenv("OWNER_EMAIL", "cache-owner@zakowhat.uz")
    owner = _user(db_session, "cache-owner@zakowhat.uz", "Boshlovchi")
    pack = make_classic_pack(db_session, "Kesh")
    code = anon.post("/api/rooms", json={"quiz_id": pack.id}, headers=_hdr(owner)).json()["code"]
    calls = []
    real = rooms.get_manifest_or_db_rounds
    monkeypatch.setattr(rooms, "get_manifest_or_db_rounds", lambda v, db: calls.append(v.id) or real(v, db))
    for _ in range(5):
        assert anon.get(f"/api/rooms/{code}/screen").status_code == 200
    assert len(calls) <= 1
