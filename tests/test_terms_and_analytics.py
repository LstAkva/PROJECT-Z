"""
Recorded agreement to the Privacy Policy / Terms at sign-up (time + document version), and the owner's
first-party analytics (aggregate counts only, owner-only, previews and demonstrations not counted).
"""
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import status

from api.auth import TERMS_VERSION
from models import DailyQuestion, DailySubmission, QuizVersion, SoloAttempt, User
from tests.test_player_progress import CLASSIC_Q, _answer_classic, _finish, _hdr, _start, _user, make_classic_pack

OWNER_EMAIL = "owner-analytics@zakowhat.uz"
CLIENT_ID = "mock-client-id.apps.googleusercontent.com"


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


@pytest.fixture
def owner(db_session, monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", OWNER_EMAIL)
    return _user(db_session, OWNER_EMAIL, "Tizim Egasi")


def _register(client, **extra):
    body = {"email": "yangi@misol.uz", "password": "Parol123!", "display_name": "Yangi O'yinchi", **extra}
    return client.post("/api/auth/register", json=body)


def _google(client, email, sub, **extra):
    info = {"sub": sub, "email": email, "name": "Google User", "email_verified": True}
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=info):
        return client.post("/api/auth/google", json={"credential": "mock-jwt-token", **extra})


# --- agreement recorded at sign-up -------------------------------------------------------------------------

def test_register_records_the_agreement(anon, db_session):
    assert _register(anon, accept_terms=True).status_code == status.HTTP_201_CREATED
    user = db_session.query(User).filter_by(email="yangi@misol.uz").one()
    assert user.terms_version == TERMS_VERSION and user.terms_accepted_at is not None


def test_register_refuses_an_explicit_no(anon, db_session):
    res = _register(anon, accept_terms=False)
    assert res.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "rozilik" in res.json()["detail"]
    assert db_session.query(User).filter_by(email="yangi@misol.uz").count() == 0


def test_register_from_an_older_client_records_nothing(anon, db_session):
    assert _register(anon).status_code == status.HTTP_201_CREATED
    user = db_session.query(User).filter_by(email="yangi@misol.uz").one()
    assert user.terms_accepted_at is None and user.terms_version is None


def test_google_sign_up_records_the_agreement(anon, db_session, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    assert _google(anon, "g-new@misol.uz", "g-sub-1", accept_terms=True).status_code == 200
    user = db_session.query(User).filter_by(email="g-new@misol.uz").one()
    assert user.terms_version == TERMS_VERSION and user.terms_accepted_at is not None


def test_google_sign_in_fills_a_missing_record_but_keeps_an_existing_one(anon, db_session, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    old = _user(db_session, "old@misol.uz", "Eski")
    assert old.terms_accepted_at is None
    assert _google(anon, "old@misol.uz", "g-sub-2", accept_terms=True).status_code == 200
    db_session.refresh(old)
    first = old.terms_accepted_at
    assert first is not None and old.terms_version == TERMS_VERSION

    old.terms_version = "2026-01-01"
    db_session.commit()
    assert _google(anon, "old@misol.uz", "g-sub-2", accept_terms=True).status_code == 200
    db_session.refresh(old)
    assert old.terms_version == "2026-01-01" and old.terms_accepted_at == first


def test_export_includes_the_agreement(anon, db_session):
    _register(anon, accept_terms=True)
    user = db_session.query(User).filter_by(email="yangi@misol.uz").one()
    account = anon.get("/api/me/export", headers=_hdr(user)).json()["account"]
    assert account["terms_version"] == TERMS_VERSION and account["terms_accepted_at"]


def test_both_sign_up_forms_ask_and_send_the_agreement(anon):
    arena = anon.get("/arena").text
    assert 'id="arena-register-consent"' in arena and "consentBox.checked" in arena
    assert arena.count("accept_terms: true") == 2          # password sign-up and Google
    assert "Google orqali davom etib" in arena
    modal = anon.get("/").text
    assert 'id="register-consent"' in modal and modal.count("accept_terms: true") >= 2


def test_migrations_have_a_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1   # the live-rooms migration (d4e5f6a7b8c9) follows the terms one


# --- owner analytics ---------------------------------------------------------------------------------------

def test_analytics_is_owner_only(anon, owner, db_session):
    assert anon.get("/api/owner/analytics").status_code == status.HTTP_401_UNAUTHORIZED
    player = _user(db_session, "not-owner@misol.uz", "Oddiy")
    assert anon.get("/api/owner/analytics", headers=_hdr(player)).status_code == status.HTTP_403_FORBIDDEN
    assert anon.get("/api/owner/analytics?days=3", headers=_hdr(owner)).status_code == 422


def test_analytics_counts_games_users_and_daily(anon, owner, db_session):
    quiz = make_classic_pack(db_session, "Statistika Pack")
    a = _user(db_session, "stat-a@misol.uz", "Stat A")
    b = _user(db_session, "stat-b@misol.uz", "Stat B")
    old = _user(db_session, "stat-old@misol.uz", "Stat Old")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=120)
    db_session.commit()

    done = _start(anon, quiz, a)
    _answer_classic(anon, done, a, CLASSIC_Q)
    _finish(anon, done, a)
    _start(anon, quiz, b)                                     # started, not finished
    version = db_session.query(QuizVersion).filter_by(quiz_id=quiz.id).first()
    db_session.add(SoloAttempt(quiz_version_id=version.id, user_id=owner.id, timer_mode="preview"))
    dq = DailyQuestion(schedule_date=date.today(), question_id="stat-1", message_id=9, channel_id=1,
                       question_text="Savol", primary_answer="Javob")
    db_session.add(dq)
    db_session.commit()
    db_session.add_all([
        DailySubmission(daily_question_id=dq.id, user_id=a.id, submitted_text="Javob", is_correct=True),
        DailySubmission(daily_question_id=dq.id, anon_id="anon-x", submitted_text="xato", is_correct=False),
    ])
    db_session.commit()

    res = anon.get("/api/owner/analytics?days=30", headers=_hdr(owner))
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["period"]["days"] == 30 and len(d["series"]) == 30 and d["period"]["timezone"] == "Asia/Tashkent"
    assert d["games"]["started"] == 2 and d["games"]["completed"] == 1           # the preview is not a game
    assert d["games"]["completion_rate"] == 0.5 and d["games"]["active_players"] == 2
    total = db_session.query(User).count()                  # includes any author account the pack helper made
    assert d["users"]["total"] == total and d["users"]["new_in_period"] == total - 1   # not the 120-day-old one
    assert d["users"]["by_sign_in_method"] == {"local": total}
    assert d["daily_question"]["answers"] == 2 and d["daily_question"]["correct_rate"] == 0.5
    assert d["daily_question"]["players"] == 2
    assert d["top_packs"] == [{"quiz_id": quiz.id, "title": "Statistika Pack", "started": 2, "completed": 1}]
    assert sum(day["games_started"] for day in d["series"]) == 2
    assert sum(day["new_users"] for day in d["series"]) == total - 1
    text = json.dumps(d, ensure_ascii=False)
    assert "@misol.uz" not in text and "Stat A" not in text and OWNER_EMAIL not in text


def test_owner_page_has_the_analytics_tab(anon, owner):
    page = anon.get("/owner", headers=_hdr(owner)).text
    assert 'id="nav-tab-analytics"' in page and 'id="owner-view-analytics"' in page
    assert "/api/owner/analytics?days=" in page


# --- owner pages are not shown to anyone else ----------------------------------------------------------------

@pytest.mark.parametrize("path", ["/owner", "/owner/users", "/bank", "/admin", "/builder", "/builder/3", "/create"])
def test_owner_pages_send_everyone_else_to_the_arena(anon, owner, db_session, path):
    res = anon.get(path, follow_redirects=False)
    assert res.status_code == status.HTTP_303_SEE_OTHER and res.headers["location"] == "/arena"
    assert res.headers["cache-control"] == "no-store"
    player = _user(db_session, f"curious-{abs(hash(path)) % 10000}@misol.uz", "Qiziquvchan")
    res = anon.get(path, headers=_hdr(player), follow_redirects=False)
    assert res.status_code == status.HTTP_303_SEE_OTHER and res.headers["location"] == "/arena"
    assert "Owner" not in res.text and "owner" not in res.text


def test_owner_pages_fail_closed_without_owner_config(anon, owner, monkeypatch):
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    monkeypatch.delenv("OWNER_EMAILS", raising=False)
    res = anon.get("/owner", headers=_hdr(owner), follow_redirects=False)
    assert res.status_code == status.HTTP_303_SEE_OTHER and res.headers["location"] == "/arena"


def test_owner_gets_the_owner_page_uncached(anon, owner):
    res = anon.get("/owner", headers=_hdr(owner), follow_redirects=False)
    assert res.status_code == 200 and 'id="nav-tab-analytics"' in res.text
    assert res.headers["cache-control"] == "no-store" and res.headers["x-robots-tag"] == "noindex, nofollow"
