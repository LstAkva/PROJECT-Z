"""
Jonli xona (live rooms): owner-only hosting, joining with a code and a team name (no account), answering
during the clock, answer revealed only by the host, host overrules, team removal, results export, retention,
and rooms never touching ranked play or statistics.
"""
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status

from models import LiveAnswer, LiveRoom, LiveTeam, QuizVersion, SoloAttempt
from tests.test_player_progress import CLASSIC_Q, _hdr, _user, make_classic_pack

OWNER_EMAIL = "rooms-owner@zakowhat.uz"


@pytest.fixture(autouse=True)
def phone_joining_on(monkeypatch):
    """Most tests here cover joining from phones; presentation mode (the default) has its own tests below."""
    monkeypatch.setenv("ROOMS_PLAYER_JOIN", "1")


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


@pytest.fixture
def owner(db_session, monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", OWNER_EMAIL)
    monkeypatch.delenv("OWNER_EMAILS", raising=False)
    return _user(db_session, OWNER_EMAIL, "Boshlovchi")


@pytest.fixture
def pack(db_session):
    return make_classic_pack(db_session, "Fakultetlar Kubogi")


def create(anon, owner, pack, **extra):
    res = anon.post("/api/rooms", json={"quiz_id": pack.id, **extra}, headers=_hdr(owner))
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()["code"]


def join(anon, code, name):
    res = anon.post("/api/rooms/join", json={"code": code, "team_name": name})
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()["token"]


def host(anon, owner, code, action, body=None, expect=200):
    res = anon.post(f"/api/rooms/{code}/{action}", json=body or {}, headers=_hdr(owner))
    assert res.status_code == expect, res.text
    return res.json()


def me(anon, code, token, expect=200):
    res = anon.get(f"/api/rooms/{code}/me", headers={"X-Team-Token": token})
    assert res.status_code == expect, res.text
    return res.json()


def answer(anon, code, token, text, expect=200):
    res = anon.post(f"/api/rooms/{code}/answer", json={"text": text}, headers={"X-Team-Token": token})
    assert res.status_code == expect, res.text
    return res.json()


# --- hosting -------------------------------------------------------------------------------------------------

def test_only_the_owner_can_create_and_run_rooms(anon, owner, pack, db_session):
    assert anon.post("/api/rooms", json={"quiz_id": pack.id}).status_code == status.HTTP_401_UNAUTHORIZED
    player = _user(db_session, "rooms-player@misol.uz", "O‘yinchi")
    assert anon.post("/api/rooms", json={"quiz_id": pack.id}, headers=_hdr(player)).status_code == 403
    code = create(anon, owner, pack)
    assert len(code) == 6 and code.isalnum() and code.isupper() and not set(code) & set("0O1I")
    assert anon.post(f"/api/rooms/{code}/start", headers=_hdr(player)).status_code == 403
    assert anon.get(f"/api/rooms/{code}/host", headers=_hdr(player)).status_code == 403
    assert anon.get(f"/api/rooms/{code}/export").status_code == 401


def test_a_room_is_hosted_only_by_its_creator(anon, owner, pack, db_session, monkeypatch):
    other = _user(db_session, "second-owner@zakowhat.uz", "Ikkinchi")
    monkeypatch.setenv("OWNER_EMAILS", "second-owner@zakowhat.uz")
    code = create(anon, owner, pack)
    assert anon.get(f"/api/rooms/{code}/host", headers=_hdr(other)).status_code == 404


def test_svoya_igra_packs_and_unknown_packs_are_refused(anon, owner, pack, db_session):
    version = db_session.query(QuizVersion).filter_by(quiz_id=pack.id).one()
    version.game_mode = "svoyak"
    db_session.commit()
    res = anon.post("/api/rooms", json={"quiz_id": pack.id}, headers=_hdr(owner))
    assert res.status_code == 400 and "Zakovat" in res.json()["detail"]
    assert anon.post("/api/rooms", json={"quiz_id": 99999}, headers=_hdr(owner)).status_code == 404
    assert anon.post("/api/rooms", json={"quiz_id": pack.id, "seconds_per_question": 5}, headers=_hdr(owner)).status_code == 422


# --- joining ------------------------------------------------------------------------------------------------

def test_join_with_code_and_team_name(anon, owner, pack):
    code = create(anon, owner, pack)
    token = join(anon, code, "  Iqtisodchilar   jamoasi ")
    state = me(anon, code, token)
    assert state["team"]["name"] == "Iqtisodchilar jamoasi" and state["status"] == "lobby"
    assert state["question"] is None and state["teams_count"] == 1
    # lower-case code with spaces works too
    assert anon.post("/api/rooms/join", json={"code": code.lower()[:3] + " " + code.lower()[3:], "team_name": "Huquqchilar"}).status_code == 201


def test_join_rejects_duplicates_bad_codes_and_finished_rooms(anon, owner, pack):
    code = create(anon, owner, pack)
    join(anon, code, "Fiziklar")
    res = anon.post("/api/rooms/join", json={"code": code, "team_name": "FIZIKLAR"})
    assert res.status_code == 409 and "nomdagi" in res.json()["detail"]
    assert anon.post("/api/rooms/join", json={"code": code, "team_name": "   "}).status_code == 422
    assert anon.post("/api/rooms/join", json={"code": "ZZZZZZ", "team_name": "X"}).status_code == 404
    host(anon, owner, code, "end")
    assert anon.post("/api/rooms/join", json={"code": code, "team_name": "Kechikkanlar"}).status_code == 409


def test_player_endpoints_need_the_team_token_of_that_room(anon, owner, pack):
    code = create(anon, owner, pack)
    other_code = create(anon, owner, pack)
    token = join(anon, code, "Kimyogarlar")
    assert anon.get(f"/api/rooms/{code}/me").status_code == 401
    assert anon.get(f"/api/rooms/{other_code}/me", headers={"X-Team-Token": token}).status_code == 401
    assert anon.get(f"/api/rooms/{code}/me", headers={"X-Team-Token": "forged"}).status_code == 401


def test_team_tokens_are_stored_hashed(anon, owner, pack, db_session):
    code = create(anon, owner, pack)
    token = join(anon, code, "Tarixchilar")
    team = db_session.query(LiveTeam).filter_by(name="Tarixchilar").one()
    assert token not in team.token_hash and len(team.token_hash) == 64


# --- playing ------------------------------------------------------------------------------------------------

def test_full_game_flow(anon, owner, pack):
    code = create(anon, owner, pack, seconds_per_question=30)
    a, b = join(anon, code, "A jamoa"), join(anon, code, "B jamoa")
    answer(anon, code, a, "Javob 1", expect=409)                      # not started yet

    state = host(anon, owner, code, "start")
    assert state["status"] == "question" and state["question_number"] == 1 and state["total_questions"] == CLASSIC_Q
    assert 28 <= state["remaining_seconds"] <= 30 and state["host_question"]["correct_answer"] == "Javob 1"

    screen = anon.get(f"/api/rooms/{code}/screen").json()
    assert screen["question"]["text"].endswith("savol 1") and screen["reveal"] is None
    assert "Javob 1" not in str(screen)                              # the answer stays hidden until revealed
    assert screen["answered_count"] == 0 and screen["teams_count"] == 2
    player_view = me(anon, code, a)
    assert player_view["answer_open"] and "Javob 1" not in str(player_view)

    answer(anon, code, a, "javob  1")                                 # normalised match
    answer(anon, code, b, "Noto‘g‘ri")
    answer(anon, code, b, "Javob 1")                                  # may change until time is up
    answer(anon, code, b, "boshqa")
    assert anon.get(f"/api/rooms/{code}/screen").json()["answered_count"] == 2

    host(anon, owner, code, "next", expect=409)                       # reveal first
    state = host(anon, owner, code, "reveal")
    assert state["status"] == "reveal"
    answer(anon, code, a, "Javob 1", expect=409)                      # closed after the reveal
    va, vb = me(anon, code, a), me(anon, code, b)
    assert va["reveal"]["correct_answer"] == "Javob 1" and va["my_correct"] is True and va["my_points"] == 1
    assert vb["my_correct"] is False and vb["my_answer"] == "boshqa" and vb["my_rank"] == 2
    assert [r["name"] for r in va["standings"]] == ["A jamoa", "B jamoa"]

    for _ in range(CLASSIC_Q - 1):
        host(anon, owner, code, "next")
        host(anon, owner, code, "reveal")
    state = host(anon, owner, code, "next")
    assert state["status"] == "finished"
    final = me(anon, code, a)
    assert final["status"] == "finished" and final["standings"][0]["name"] == "A jamoa"


def test_answers_close_when_the_clock_runs_out(anon, owner, pack, db_session):
    code = create(anon, owner, pack)
    token = join(anon, code, "Kech qolganlar")
    host(anon, owner, code, "start")
    room = db_session.query(LiveRoom).filter_by(code=code).one()
    room.question_deadline = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(tzinfo=None)
    db_session.commit()
    assert me(anon, code, token)["answer_open"] is False
    res = answer(anon, code, token, "Javob 1", expect=409)
    assert "vaqti tugadi" in res["detail"]


def test_pause_resume_and_extra_time(anon, owner, pack):
    code = create(anon, owner, pack, seconds_per_question=40)
    token = join(anon, code, "Sabrlilar")
    host(anon, owner, code, "start")
    state = host(anon, owner, code, "timer", {"action": "pause"})
    assert state["paused"] is True and 38 <= state["remaining_seconds"] <= 40
    answer(anon, code, token, "Javob 1")                              # answering still works while paused
    state = host(anon, owner, code, "timer", {"action": "add", "seconds": 15})
    assert 53 <= state["remaining_seconds"] <= 55
    state = host(anon, owner, code, "timer", {"action": "resume"})
    assert state["paused"] is False and 52 <= state["remaining_seconds"] <= 55
    host(anon, owner, code, "timer", {"action": "explode"}, expect=422)


def test_host_can_overrule_the_automatic_check(anon, owner, pack):
    code = create(anon, owner, pack)
    a = join(anon, code, "Apellyatsiya")
    host(anon, owner, code, "start")
    answer(anon, code, a, "Birinchi javob")                            # automatic check: wrong
    state = host(anon, owner, code, "reveal")
    item = state["answers"][0]
    assert item["auto_correct"] is False and item["correct"] is False
    state = host(anon, owner, code, f"answers/{item['id']}/verdict", {"correct": True})
    assert state["answers"][0]["correct"] is True and state["standings"][0]["points"] == 1
    assert me(anon, code, a)["my_correct"] is True
    state = host(anon, owner, code, f"answers/{item['id']}/verdict", {"correct": None})
    assert state["answers"][0]["correct"] is False and state["standings"][0]["points"] == 0


def test_removed_team_is_locked_out_and_left_off_the_table(anon, owner, pack, db_session):
    code = create(anon, owner, pack)
    good, bad = join(anon, code, "Odobli"), join(anon, code, "Shovqinchilar")
    team_id = db_session.query(LiveTeam).filter_by(name="Shovqinchilar").one().id
    state = host(anon, owner, code, f"teams/{team_id}/remove")
    assert [t["name"] for t in state["teams"]] == ["Odobli"]
    res = anon.get(f"/api/rooms/{code}/me", headers={"X-Team-Token": bad})
    assert res.status_code == 403
    assert me(anon, code, good)["teams_count"] == 1


def test_export_is_owner_only_csv_and_safe_for_spreadsheets(anon, owner, pack):
    code = create(anon, owner, pack)
    a = join(anon, code, "=HYPERLINK(1)")
    join(anon, code, "Oddiy")
    host(anon, owner, code, "start")
    answer(anon, code, a, "Javob 1")
    host(anon, owner, code, "reveal")
    res = anon.get(f"/api/rooms/{code}/export", headers=_hdr(owner))
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert f'zakowhat-xona-{code}.csv' in res.headers["content-disposition"] and res.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(res.text.lstrip("﻿"))))
    assert rows[0][:3] == ["O‘rin", "Jamoa", "Ball"] and len(rows[0]) == 3 + CLASSIC_Q
    assert rows[1][1] == "'=HYPERLINK(1)" and rows[1][2] == "1" and rows[1][3] == "1"


# --- isolation and retention -------------------------------------------------------------------------------

def test_rooms_never_create_games_or_statistics(anon, owner, pack, db_session):
    before = db_session.query(SoloAttempt).count()
    code = create(anon, owner, pack)
    token = join(anon, code, "Statistikasiz")
    host(anon, owner, code, "start")
    answer(anon, code, token, "Javob 1")
    host(anon, owner, code, "reveal")
    assert db_session.query(SoloAttempt).count() == before
    assert anon.get(f"/api/quizzes/{pack.id}/leaderboard").json().get("entries", []) == []


def test_rooms_older_than_30_days_are_deleted(anon, owner, pack, db_session):
    old_code = create(anon, owner, pack)
    join(anon, old_code, "Eski jamoa")
    room = db_session.query(LiveRoom).filter_by(code=old_code).one()
    room.created_at = (datetime.now(timezone.utc) - timedelta(days=31)).replace(tzinfo=None)
    db_session.commit()
    create(anon, owner, pack)                                          # cleanup runs when a room is created
    db_session.expire_all()
    assert db_session.query(LiveRoom).filter_by(code=old_code).first() is None
    assert db_session.query(LiveTeam).filter_by(name="Eski jamoa").first() is None


# --- pages --------------------------------------------------------------------------------------------------

def test_room_pages(anon, owner, pack, db_session):
    code = create(anon, owner, pack)
    for path in ("/join", f"/room/{code}", f"/room/{code}/screen"):
        res = anon.get(path)
        assert res.status_code == 200 and res.headers["x-robots-tag"] == "noindex, nofollow", path
        assert res.headers["cache-control"] == "no-store"
    assert anon.get(f"/room/{code}/host", follow_redirects=False).headers["location"] == "/arena"
    player = _user(db_session, "page-player@misol.uz", "Sahifa")
    assert anon.get(f"/room/{code}/host", headers=_hdr(player), follow_redirects=False).status_code == 303
    page = anon.get(f"/room/{code}/host", headers=_hdr(owner))
    assert page.status_code == 200 and code in page.text
    assert "/room/" in anon.get("/robots.txt").text and "/join" in anon.get("/robots.txt").text


def test_owner_panel_has_the_rooms_tab(anon, owner):
    page = anon.get("/owner", headers=_hdr(owner)).text
    assert 'id="nav-tab-rooms"' in page and "/api/rooms/mine" in page


# --- presentation mode (phone joining off, the default) --------------------------------------------------------

def test_phone_joining_is_off_by_default(anon, owner, pack, monkeypatch):
    monkeypatch.delenv("ROOMS_PLAYER_JOIN", raising=False)
    code = create(anon, owner, pack)
    res = anon.post("/api/rooms/join", json={"code": code, "team_name": "Kimdir"})
    assert res.status_code == 404 and "yoqilmagan" in res.json()["detail"]
    assert anon.get(f"/api/rooms/{code}/me", headers={"X-Team-Token": "x"}).status_code == 404
    assert anon.post(f"/api/rooms/{code}/answer", json={"text": "a"}, headers={"X-Team-Token": "x"}).status_code == 404
    assert anon.get(f"/api/rooms/{code}/qr.svg").status_code == 404
    for path in ("/join", f"/room/{code}"):
        res = anon.get(path, follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"] == "/arena", path
    assert 'href="/join"' not in anon.get("/").text


def test_presentation_mode_runs_without_teams(anon, owner, pack, monkeypatch):
    monkeypatch.delenv("ROOMS_PLAYER_JOIN", raising=False)
    code = create(anon, owner, pack)
    state = host(anon, owner, code, "start")
    assert state["player_join"] is False and state["status"] == "question"
    screen = anon.get(f"/api/rooms/{code}/screen").json()
    assert screen["player_join"] is False and "answered_count" not in screen and screen["question"]["text"]
    host(anon, owner, code, "reveal")
    screen = anon.get(f"/api/rooms/{code}/screen").json()
    assert screen["reveal"]["correct_answer"] == "Javob 1" and screen["standings"] is None
    assert anon.get(f"/room/{code}/screen").status_code == 200


def test_qr_code_points_to_the_join_link(anon, owner, pack):
    code = create(anon, owner, pack)
    res = anon.get(f"/api/rooms/{code}/qr.svg")
    assert res.status_code == 200 and res.headers["content-type"].startswith("image/svg+xml")
    assert res.text.lstrip().startswith("<svg") and "<path" in res.text
    import segno
    assert segno.make(f"https://zakowhat.uz/join?code={code}", error="m").version >= 1
    assert 'href="/join"' in anon.get("/").text                                 # footer link while joining is on


# --- host role (HOST_EMAILS) -------------------------------------------------------------------------------------

def test_hosts_run_rooms_but_get_nothing_else(anon, owner, pack, db_session, monkeypatch):
    monkeypatch.setenv("HOST_EMAILS", "professor@misol.uz, other-host@misol.uz")
    prof = _user(db_session, "professor@misol.uz", "Professor")
    packs = anon.get("/api/rooms/packs", headers=_hdr(prof))
    assert packs.status_code == 200 and packs.json()["packs"][0]["title"] == "Fakultetlar Kubogi"
    code = create(anon, prof, pack)
    assert host(anon, prof, code, "start")["status"] == "question"
    assert anon.get("/host", headers=_hdr(prof)).status_code == 200
    assert anon.get(f"/room/{code}/host", headers=_hdr(prof)).status_code == 200
    # nothing of the owner panel
    assert anon.get("/api/owner/users", headers=_hdr(prof)).status_code == 403
    assert anon.get("/owner", headers=_hdr(prof), follow_redirects=False).status_code == 303
    # rooms stay with their host: the owner does not see or steer the professor's room
    assert anon.get(f"/api/rooms/{code}/host", headers=_hdr(owner)).status_code == 404
    assert [r["code"] for r in anon.get("/api/rooms/mine", headers=_hdr(prof)).json()["rooms"]] == [code]


def test_host_role_fails_closed(anon, pack, db_session, monkeypatch):
    monkeypatch.delenv("HOST_EMAILS", raising=False)
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    monkeypatch.delenv("OWNER_EMAILS", raising=False)
    someone = _user(db_session, "nobody-special@misol.uz", "Oddiy")
    assert anon.post("/api/rooms", json={"quiz_id": pack.id}, headers=_hdr(someone)).status_code == 403
    assert anon.get("/api/rooms/packs", headers=_hdr(someone)).status_code == 403
    assert anon.get("/host", headers=_hdr(someone), follow_redirects=False).headers["location"] == "/arena"


def test_console_and_screen_take_keyboard_and_clicker(anon, owner, pack):
    code = create(anon, owner, pack)
    console = anon.get(f"/room/{code}/host", headers=_hdr(owner)).text
    screen = anon.get(f"/room/{code}/screen").text
    assert "R.hostKeys(CODE" in console and "R.hostKeys(CODE" in screen
    js = anon.get("/static/js/zakowhat-rooms.js").text
    for key in ("'ArrowRight'", "'PageDown'", "'p'", "now - last < 700"):
        assert key in js, key
