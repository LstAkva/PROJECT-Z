"""
Privacy pack: "download my data" export (own data only, no secrets, no spoilers), profile pictures with
unguessable names that contain no user id, and HSTS on HTTPS responses.
"""
import base64
import json
import os
from datetime import date

import pytest
from fastapi import status

from models import DailyQuestion, DailySubmission
from tests.test_player_progress import CLASSIC_Q, _answer_classic, _finish, _hdr, _start, _user, make_classic_pack


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


@pytest.fixture
def player(db_session):
    return _user(db_session, "exporter@zakowhat.uz", "Eksport Egasi")


def export(client, user):
    res = client.get("/api/me/export", headers=_hdr(user))
    assert res.status_code == status.HTTP_200_OK, res.text
    return res


def test_export_requires_sign_in(anon):
    assert anon.get("/api/me/export").status_code == status.HTTP_401_UNAUTHORIZED


def test_export_is_a_private_download(anon, player):
    res = export(anon, player)
    assert res.headers["content-disposition"].startswith('attachment; filename="zakowhat-ma-lumotlarim-')
    assert res.headers["cache-control"] == "no-store"
    data = res.json()
    assert data["account"]["email"] == "exporter@zakowhat.uz" and data["account"]["display_name"] == "Eksport Egasi"
    assert data["account"]["has_password"] is True
    text = res.text
    for secret in ("hashed_password", "$2b$", "access_token", "session_token", "google_id\""):
        assert secret not in text, secret


def test_export_contains_own_games_and_answers_only(anon, db_session, player):
    other = _user(db_session, "someone-else@zakowhat.uz", "Boshqa Odam")
    quiz = make_classic_pack(db_session, "Eksport Pack")
    mine = _start(anon, quiz, player)
    _answer_classic(anon, mine, player, CLASSIC_Q, wrong={1})
    _finish(anon, mine, player)
    theirs = _start(anon, quiz, other)
    _answer_classic(anon, theirs, other, 2)

    data = export(anon, player).json()
    assert len(data["games"]) == 1
    game = data["games"][0]
    assert game["pack"] == "Eksport Pack" and game["type"] == "ranked" and game["status"] == "completed"
    assert len(game["answers"]) == CLASSIC_Q
    first = game["answers"][0]
    assert first["question"].startswith("Eksport Pack savol 1") and first["your_answer"] == "xato" and first["correct"] is False
    text = json.dumps(data, ensure_ascii=False)
    assert "Boshqa Odam" not in text and "someone-else@" not in text
    # the correct answer of a question the player got wrong is not revealed
    assert '"Javob 1"' not in text


def test_export_daily_answers_without_the_daily_answer(anon, db_session, player):
    dq = DailyQuestion(schedule_date=date(2026, 11, 20), question_id="qa-1", message_id=1, channel_id=1,
                       question_text="Kunning maxfiy savoli", primary_answer="MaxfiyJavob")
    db_session.add(dq)
    db_session.commit()
    db_session.add(DailySubmission(daily_question_id=dq.id, user_id=player.id, submitted_text="taxmin", is_correct=False))
    db_session.commit()
    data = export(anon, player).json()
    assert data["daily_question"] == [{"date": "2026-11-20", "your_answer": "taxmin", "correct": False,
                                       "answered_at": data["daily_question"][0]["answered_at"]}]
    assert "MaxfiyJavob" not in json.dumps(data, ensure_ascii=False)


def test_profile_picture_names_hide_the_user_id(anon, player):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    res = anon.post("/api/auth/avatar", headers=_hdr(player),
                    json={"image_data": "data:image/png;base64," + base64.b64encode(png).decode("ascii")})
    assert res.status_code == 200, res.text
    url = res.json()["avatar_url"]
    name = url.rsplit("/", 1)[1]
    assert name.startswith("avatar_") and f"u{player.id}_" not in name and len(name) >= len("avatar_") + 32
    path = url.lstrip("/")
    if os.path.exists(path):
        os.remove(path)


def test_hsts_only_over_https(anon):
    assert anon.get("/health", headers={"X-Forwarded-Proto": "https"}).headers.get("strict-transport-security") == "max-age=15552000"
    assert "strict-transport-security" not in anon.get("/health").headers


def test_settings_offer_the_download(anon):
    assert 'href="/api/me/export"' in anon.get("/profile").text
