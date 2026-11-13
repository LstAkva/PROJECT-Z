"""
Phase 6 — Daily engagement: streaks, the shareable result and Daily bank monitoring.

Streak arithmetic is tested directly (it is a pure function); everything else goes through the real
API with the test database, simulating days by moving the server's Asia/Tashkent date.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import status

import api.daily as daily_api
from api.auth import create_access_token, hash_password
from models import DailyQuestion, DailySubmission, User
from services.daily_engagement import (
    CRITICAL_AT,
    WARNING_AT,
    _streak_from_dates,
    bank_level,
    build_share_text,
    public_site_url,
    compute_bank_status,
)
from services.daily_question import get_or_schedule_daily_question, is_candidate_eligible

D = date  # brevity in the rule tables below


# =========================================================================== streak rules (pure)

def _streak(days, today, scheduled=None):
    days = [D.fromisoformat(d) if isinstance(d, str) else d for d in days]
    today = D.fromisoformat(today) if isinstance(today, str) else today
    if scheduled is None:          # by default every calendar day had a question
        start = min(days) if days else today
        scheduled = {start + timedelta(n) for n in range((today - start).days + 1)}
    return _streak_from_dates(days, set(scheduled), today)


def test_no_history_is_an_empty_streak():
    assert _streak([], "2026-11-13") == {"current": 0, "best": 0, "total_days": 0, "last_date": None,
                                          "today_done": False, "at_risk": False}


def test_first_participation_starts_a_streak_of_one():
    s = _streak(["2026-11-13"], "2026-11-13")
    assert (s["current"], s["best"], s["total_days"], s["today_done"], s["at_risk"]) == (1, 1, 1, True, False)


def test_consecutive_days_extend_the_streak():
    s = _streak(["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"], "2026-11-13")
    assert (s["current"], s["best"], s["total_days"]) == (4, 4, 4)


def test_yesterday_keeps_the_streak_alive_but_at_risk():
    s = _streak(["2026-11-11", "2026-11-12"], "2026-11-13")
    assert (s["current"], s["today_done"], s["at_risk"]) == (2, False, True)


def test_a_missed_scheduled_day_breaks_the_streak():
    s = _streak(["2026-11-10", "2026-11-11"], "2026-11-13")        # the 12th had a question, unanswered
    assert (s["current"], s["best"], s["at_risk"]) == (0, 2, False)


def test_playing_again_after_a_break_starts_a_new_streak_and_keeps_the_best():
    s = _streak(["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-05", "2026-11-06"], "2026-11-06")
    assert (s["current"], s["best"], s["total_days"]) == (2, 3, 5)


def test_best_streak_is_preserved_after_long_inactivity():
    s = _streak([D(2026, 1, d) for d in range(1, 11)] + ["2026-11-13"], "2026-11-13")
    assert (s["current"], s["best"], s["total_days"]) == (1, 10, 11)


def test_month_transition_is_consecutive():
    assert _streak(["2026-10-30", "2026-10-31", "2026-11-01"], "2026-11-01")["current"] == 3
    assert _streak(["2028-02-28", "2028-02-29", "2028-03-01"], "2028-03-01")["current"] == 3   # leap year


def test_year_transition_is_consecutive():
    assert _streak(["2026-12-30", "2026-12-31", "2027-01-01"], "2027-01-01")["current"] == 3


def test_days_without_a_scheduled_question_do_not_break_a_streak():
    """If the bank was empty (no question could be played), the gap is neutral."""
    days = ["2026-11-10", "2026-11-11", "2026-11-14"]
    scheduled = {D(2026, 11, 10), D(2026, 11, 11), D(2026, 11, 14)}       # 12th and 13th had no question
    s = _streak(days, "2026-11-14", scheduled)
    assert (s["current"], s["best"], s["total_days"]) == (3, 3, 3)
    # ...but a scheduled day in such a gap still counts as missed
    s = _streak(days, "2026-11-14", scheduled | {D(2026, 11, 13)})
    assert (s["current"], s["best"]) == (1, 2)


def test_duplicate_and_future_dates_are_ignored():
    s = _streak(["2026-11-12", "2026-11-12", "2026-11-13", "2026-11-20"], "2026-11-13")
    assert (s["current"], s["total_days"], s["last_date"]) == (2, 2, "2026-11-13")


# =========================================================================== API helpers

@pytest.fixture
def player(db_session):
    user = User(email=f"streak_{uuid.uuid4().hex[:8]}@zakowhat.uz", display_name="Seriya O‘yinchi",
                hashed_password=hash_password("Pass123!"), auth_provider="local")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _hdr(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


def _on(monkeypatch, day):
    """Moves the server's Asia/Tashkent date (the only clock the Daily API uses)."""
    monkeypatch.setattr(daily_api, "get_tashkent_today", lambda: day)


def _play(client, user, answer="noto‘g‘ri taxmin"):
    res = client.post("/api/daily/submit", json={"answer": answer}, headers=_hdr(user))
    assert res.status_code == status.HTTP_200_OK, res.text
    return res.json()


def _stats(client, user):
    res = client.get("/api/daily/stats", headers=_hdr(user))
    assert res.status_code == status.HTTP_200_OK, res.text
    return res.json()


# =========================================================================== streaks through the API

def test_streak_follows_real_submissions_across_days(client, db_session, player, monkeypatch):
    start = D(2026, 12, 29)
    for offset in range(4):                                        # 29, 30, 31 Dec, 1 Jan: across a year
        _on(monkeypatch, start + timedelta(offset))
        _play(client, player)
    s = _stats(client, player)
    assert (s["current"], s["best"], s["total_days"], s["last_date"], s["today_done"]) == (4, 4, 4, "2027-01-01", True)

    # 2 January: question opened by someone, but this player skips it -> still alive, at risk
    _on(monkeypatch, D(2027, 1, 2))
    client.get("/api/daily/today")                                 # anonymous visit schedules the day
    s = _stats(client, player)
    assert (s["current"], s["at_risk"], s["today_done"]) == (4, True, False)

    # 3 January: the scheduled 2nd was missed -> broken, best kept, playing restarts at 1
    _on(monkeypatch, D(2027, 1, 3))
    assert _stats(client, player)["current"] == 0
    _play(client, player)
    s = _stats(client, player)
    assert (s["current"], s["best"], s["total_days"]) == (1, 4, 5)


def test_several_attempts_on_one_day_count_once(client, db_session, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 13))
    _play(client, player, "birinchi xato")
    _play(client, player, "birinchi xato")                         # repeated answer: no attempt, no new day
    _play(client, player, "ikkinchi xato")
    _play(client, player, "uchinchi xato")                         # all three used: the day still counts once
    s = _stats(client, player)
    assert (s["current"], s["total_days"], s["today_done"]) == (1, 1, True)
    assert db_session.query(DailySubmission).filter_by(user_id=player.id).count() == 1


def test_a_wrong_answer_still_counts_as_participation(client, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 13))
    body = _play(client, player, "butunlay noto‘g‘ri")
    assert body["is_correct"] is False
    assert _stats(client, player)["current"] == 1


def test_failed_or_invalid_requests_do_not_count(client, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 13))
    empty = client.post("/api/daily/submit", json={"answer": ""}, headers=_hdr(player))
    assert empty.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    anonymous = client.post("/api/daily/submit", json={"answer": "javob"}, headers={"Authorization": "Bearer invalid"})
    assert anonymous.status_code == status.HTTP_401_UNAUTHORIZED
    assert _stats(client, player)["total_days"] == 0


def test_tashkent_midnight_decides_the_day_not_utc(client, player, monkeypatch):
    """19:30 UTC on the 28th is 00:30 on the 29th in Tashkent: that is a new Daily day."""
    import services.daily_question as dq_service

    def at(utc_moment):
        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_moment.astimezone(tz) if tz else utc_moment.replace(tzinfo=None)
        monkeypatch.setattr(dq_service, "datetime", FakeDatetime)

    at(datetime(2026, 10, 28, 18, 30, tzinfo=timezone.utc))         # 23:30 Tashkent, 28 Oct
    _play(client, player)
    at(datetime(2026, 10, 28, 19, 30, tzinfo=timezone.utc))         # 00:30 Tashkent, 29 Oct (UTC still the 28th)
    _play(client, player)
    s = _stats(client, player)
    assert (s["current"], s["total_days"], s["last_date"]) == (2, 2, "2026-10-29")


def test_the_device_clock_cannot_be_used(client, player, monkeypatch):
    """The client cannot name a date: extra fields are ignored and the server's day is used."""
    _on(monkeypatch, D(2026, 11, 13))
    res = client.post("/api/daily/submit", headers=_hdr(player),
                      json={"answer": "javob", "date": "2026-11-12", "schedule_date": "2026-11-12"})
    assert res.status_code == status.HTTP_200_OK
    s = _stats(client, player)
    assert (s["total_days"], s["last_date"]) == (1, "2026-11-13")


def test_existing_historical_submissions_are_counted(client, db_session, player, monkeypatch):
    """Rows written before streaks existed (no attempt metadata) are enough to rebuild the history."""
    for n, day in enumerate([D(2026, 11, 1), D(2026, 11, 2), D(2026, 11, 3), D(2026, 11, 5)]):
        dq = DailyQuestion(schedule_date=day, question_id=f"legacy_{n}", message_id=900000 + n,
                           question_text="Eski savol", primary_answer="Javob", accepted_answers=["Javob"])
        db_session.add(dq)
        db_session.flush()
        db_session.add(DailySubmission(daily_question_id=dq.id, user_id=player.id, submitted_text="x",
                                       is_correct=bool(n % 2), submission_meta=None))
    db_session.commit()
    _on(monkeypatch, D(2026, 11, 5))
    s = _stats(client, player)
    # The 4th had no question in this history, so it is neutral: 1, 2, 3 and 5 Nov are one streak.
    assert (s["current"], s["best"], s["total_days"]) == (4, 4, 4)


def test_streak_is_private_to_the_signed_in_player(client, db_session, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 13))
    _play(client, player)
    other = User(email="other_streak@zakowhat.uz", display_name="Boshqa", hashed_password=hash_password("Pass123!"),
                 auth_provider="local")
    db_session.add(other)
    db_session.commit()
    assert _stats(client, other)["total_days"] == 0
    client.cookies.clear()
    assert client.get("/api/daily/stats").status_code == status.HTTP_401_UNAUTHORIZED
    today = client.get("/api/daily/today").json()
    assert today["streak"] is None and "share_text" not in today


def test_today_includes_the_streak_for_signed_in_players(client, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 12))
    _play(client, player)
    _on(monkeypatch, D(2026, 11, 13))
    today = client.get("/api/daily/today", headers=_hdr(player)).json()
    assert today["streak"]["current"] == 1 and today["streak"]["at_risk"] is True
    assert "share_text" not in today                                # nothing to share before finishing


# =========================================================================== sharing

def test_share_text_after_a_win_has_date_attempts_and_streak_only(client, player, monkeypatch):
    for day in (D(2026, 11, 11), D(2026, 11, 12)):
        _on(monkeypatch, day)
        _play(client, player)
    _on(monkeypatch, D(2026, 11, 13))
    _play(client, player, "birinchi xato")
    first = client.get("/api/daily/today", headers=_hdr(player)).json()
    assert "share_text" not in first                                # unfinished: no share text yet

    win = client.post("/api/daily/submit", json={"answer": _correct_answer(client, first["question"]["id"])},
                      headers=_hdr(player))
    assert win.json()["is_correct"] is True

    today = client.get("/api/daily/today", headers=_hdr(player)).json()
    text = today["share_text"]
    assert text.splitlines()[0] == "🔥 Bugun ham Kun savolini yechdim!"
    assert "📅 13.11.2026" in text and "🎯 2-urinishda topdim" in text and "🔥 3 kunlik streak!" in text
    assert text.rstrip().endswith("/daily")
    _assert_nothing_private(text, today, player)


def test_share_text_without_a_streak_omits_the_streak_line(client, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 13))
    for attempt in ("xato bir", "xato ikki", "xato uch"):
        _play(client, player, attempt)
    today = client.get("/api/daily/today", headers=_hdr(player)).json()
    text = today["share_text"]
    assert "streak" not in text
    assert "🎯 3 ta urinishda topa olmadim" in text
    assert text.splitlines()[0] == "🧠 Bugungi Kun savoli oson emas edi."
    _assert_nothing_private(text, today, player)


@pytest.mark.parametrize("attempts,correct,streak,expected,absent", [
    (1, True, 0, ["🧠 Bugun Kun savolini yechdim!", "🎯 Birinchi urinishda topdim"], ["streak"]),
    (1, True, 1, ["🎯 Birinchi urinishda topdim"], ["streak", "Bugun ham"]),
    (3, True, 7, ["🔥 Bugun ham", "🎯 3-urinishda topdim", "🔥 7 kunlik streak!"], []),
    (None, True, 2, ["🔥 2 kunlik streak!"], ["urinishda"]),           # no attempt count: none invented
    (3, False, 4, ["🎯 3 ta urinishda topa olmadim", "🔥 4 kunlik streak!"], ["yechdim"]),
])
def test_share_text_lines(attempts, correct, streak, expected, absent):
    text = build_share_text(schedule_date=D(2026, 1, 5), is_correct=correct, attempts_used=attempts,
                            max_attempts=3, current_streak=streak)
    assert "📅 05.01.2026" in text and "ZakoWhat — Intelligence Meets Culture." in text
    for piece in expected:
        assert piece in text, piece
    for piece in absent:
        assert piece not in text, piece


def _correct_answer(client, question_id):
    from tests.conftest import TestingSessionLocal
    s = TestingSessionLocal()
    try:
        return s.get(DailyQuestion, question_id).primary_answer
    finally:
        s.close()


def _assert_nothing_private(text, today_payload, user):
    from tests.conftest import TestingSessionLocal
    s = TestingSessionLocal()
    try:
        dq = s.get(DailyQuestion, today_payload["question"]["id"])
        for secret in (dq.primary_answer, dq.question_text[:25], dq.explanation or "§never§", user.email,
                       user.display_name, dq.question_id):
            assert secret not in text, secret
        for answer in (dq.accepted_answers or []):
            assert answer not in text
        # Structure: every line is one of the known templates, so no id or other value can slip in.
        allowed = [r"(🔥 Bugun ham|🧠 Bugun) Kun savolini yechdim!", r"🧠 Bugungi Kun savoli oson emas edi\.",
                   r"📅 \d{2}\.\d{2}\.\d{4}", r"🎯 (Birinchi urinishda topdim|\d-urinishda topdim|\d ta urinishda topa olmadim)",
                   r"🔥 \d+ kunlik streak!", r"", r"ZakoWhat — Intelligence Meets Culture\.", r"https?://[\w.:-]+/daily"]
        for line in text.splitlines():
            assert any(re.fullmatch(p, line) for p in allowed), line
    finally:
        s.close()


# =========================================================================== history no longer leaks

def test_history_hides_todays_answer_until_the_player_finishes(client, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 13))
    _play(client, player, "birinchi xato")
    item = client.get("/api/daily/history", headers=_hdr(player)).json()["history"][0]
    assert item["primary_answer"] is None and item["explanation"] is None and item["is_finished"] is False
    for attempt in ("ikkinchi xato", "uchinchi xato"):
        _play(client, player, attempt)
    item = client.get("/api/daily/history", headers=_hdr(player)).json()["history"][0]
    assert item["primary_answer"] and item["is_finished"] is True


def test_history_reveals_past_days_even_if_unfinished(client, player, monkeypatch):
    _on(monkeypatch, D(2026, 11, 12))
    _play(client, player, "bitta xato")
    _on(monkeypatch, D(2026, 11, 13))
    item = client.get("/api/daily/history", headers=_hdr(player)).json()["history"][0]
    assert item["primary_answer"]                                     # the day is over


# =========================================================================== bank monitoring

POOL_ENTRY = {"clean_text": "Toza savol matni?", "primary_answer": "Javob", "accepted_answers": ["Javob"]}


def _entry(n, **over):
    e = dict(POOL_ENTRY, question_id=f"q{n}", message_id=n)
    e.update(over)
    return e


@pytest.fixture
def owner(client, monkeypatch):
    email = f"owner_{uuid.uuid4().hex[:8]}@example.test"
    monkeypatch.setenv("OWNER_EMAIL", email)
    client.cookies.clear()
    res = client.post("/api/auth/register", json={"email": email, "password": "ValidPassword123!", "display_name": "Owner"})
    assert res.status_code == status.HTTP_201_CREATED
    return email


@pytest.mark.parametrize("remaining,level", [(1000, "normal"), (31, "normal"), (30, "warning"), (11, "warning"),
                                             (10, "critical"), (1, "critical"), (0, "empty")])
def test_bank_levels_follow_the_thresholds(remaining, level):
    assert (WARNING_AT, CRITICAL_AT) == (30, 10)
    assert bank_level(remaining) == level


def test_bank_status_counts_like_the_scheduler(db_session):
    pool = [_entry(1), _entry(2), _entry(3), _entry(4),
            _entry(2, question_id="dup"),                          # duplicate message_id: one schedulable question
            _entry(5, clean_text="1⃣ ko‘p qismli 2⃣ savol"),        # ineligible by the filter
            _entry(6, clean_text=None, text="faqat text maydoni"),  # passes the filter, but the scheduler needs clean_text
            "not a dict"]
    today = D(2026, 11, 13)
    get_or_schedule_daily_question(db_session, target_date=D(2026, 11, 12), pool=pool)   # uses message 1
    db_session.add(DailyQuestion(schedule_date=D(2026, 11, 1), question_id="old", message_id=777,
                                 question_text="eski", primary_answer="a", accepted_answers=["a"]))
    db_session.commit()

    s = compute_bank_status(db_session, pool, today, is_candidate_eligible)
    assert (s["total_entries"], s["invalid_entries"], s["ineligible_entries"]) == (8, 2, 1)
    assert (s["eligible_entries"], s["duplicate_entries"], s["eligible_unique"]) == (5, 1, 4)
    assert (s["used"], s["scheduled_outside_bank"], s["scheduled_days_total"]) == (1, 1, 2)
    assert (s["remaining"], s["level"], s["today_scheduled"]) == (3, "critical", False)
    assert s["message"] == "Juda kam savol qoldi (3 ta)! Yangi savollar qo‘shish kerak."
    assert s["last_available_date"] == "2026-11-15"                  # 13, 14, 15 Nov, then empty

    # The count is exactly what the real scheduler can still serve.
    clean_pool = [e for e in pool if isinstance(e, dict) and e.get("clean_text")]
    served, day = 0, today
    while get_or_schedule_daily_question(db_session, target_date=day, pool=clean_pool):
        served += 1
        day += timedelta(1)
    assert served == 3
    assert compute_bank_status(db_session, pool, day, is_candidate_eligible)["level"] == "empty"


def test_bank_status_is_read_only(db_session):
    pool = [_entry(n) for n in range(1, 50)]
    before = [dict(e) for e in pool]
    s = compute_bank_status(db_session, pool, D(2026, 11, 13), is_candidate_eligible)
    assert s["remaining"] == 49 and s["level"] == "normal" and s["message"] == "Kun savoli banki: 49 ta savol qoldi."
    assert db_session.query(DailyQuestion).count() == 0               # nothing scheduled or marked
    assert pool == before                                             # bank untouched


def test_owner_endpoint_uses_the_real_bank_and_requires_owner(client, owner):
    res = client.get("/api/owner/daily-bank")
    assert res.status_code == status.HTTP_200_OK, res.text
    d = res.json()
    assert d["total_entries"] >= d["eligible_entries"] >= d["eligible_unique"] >= d["remaining"] > 0
    assert d["level"] in ("normal", "warning", "critical", "empty")
    assert d["used"] == 0 and d["remaining"] == d["eligible_unique"]

    # a regular signed-in player is refused, and so is an anonymous visitor
    client.cookies.clear()
    client.post("/api/auth/register", json={"email": "plain@zakowhat.uz", "password": "ValidPassword123!",
                                            "display_name": "Oddiy"})
    assert client.get("/api/owner/daily-bank").status_code == status.HTTP_403_FORBIDDEN
    client.cookies.clear()
    assert client.get("/api/owner/daily-bank").status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)


def test_owner_endpoint_reflects_scheduled_days(client, owner, db_session):
    before = client.get("/api/owner/daily-bank").json()
    client.get("/api/daily/today")                                   # schedules today from the real bank
    after = client.get("/api/owner/daily-bank").json()
    assert after["used"] == before["used"] + 1
    assert after["remaining"] == before["remaining"] - 1
    assert after["today_scheduled"] is True


@pytest.mark.parametrize("value,expected", [
    (None, "https://zakowhat.uz"),                       # unset: the public domain
    ("", "https://zakowhat.uz"),                         # set but empty
    ("   ", "https://zakowhat.uz"),
    ("zakowhat.uz", "https://zakowhat.uz"),              # no scheme
    ("javascript:alert(1)", "https://zakowhat.uz"),      # not a web origin
    ("https://", "https://zakowhat.uz"),
    ("https://zakowhat.uz/", "https://zakowhat.uz"),     # trailing slash trimmed
    ("https://staging.zakowhat.uz", "https://staging.zakowhat.uz"),
])
def test_public_site_url_falls_back_safely(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)
    else:
        monkeypatch.setenv("PUBLIC_SITE_URL", value)
    assert public_site_url() == expected
    text = build_share_text(schedule_date=D(2026, 11, 13), is_correct=True, attempts_used=1,
                            max_attempts=3, current_streak=0)
    assert text.splitlines()[-1] == f"{expected}/daily"
