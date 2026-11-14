"""
Owner Control Mode — demonstration sessions.

Authorization: every /api/demo endpoint is owner-only (401 anonymous, 403 everyone else, fail-closed with no
owner configured) and only acts on the calling owner's own demo sessions.
Gameplay: skip / jump / previous / replay, reveal, timer, score, restart and end work on the server.
Integrity: a demo never reaches a leaderboard, profile statistics, Arena progress, resume or Daily data, and
ranked play is unchanged around it.
"""
import time

import pytest
from fastapi import status

from api.play import is_ranked_or_legacy_timed
from models import AnswerRecord, DailySubmission, SoloAttempt
from tests.test_player_progress import CLASSIC_Q, _hdr, _start, _user, make_classic_pack
from tests.test_svoyak_gameplay import create_svoyak_test_game

OWNER_EMAIL = "owner-demo@zakowhat.uz"


@pytest.fixture
def owner(db_session, monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", OWNER_EMAIL)
    return _user(db_session, OWNER_EMAIL, "Tizim Egasi")


@pytest.fixture
def player(db_session):
    return _user(db_session, "demo-player@zakowhat.uz", "Oddiy O‘yinchi")


@pytest.fixture
def anon(client):
    client.cookies.clear()
    return client


@pytest.fixture
def pack(db_session):
    return make_classic_pack(db_session, "Namoyish Classic")


def start_demo(client, quiz, owner, **body):
    res = client.post(f"/api/demo/start/{quiz.id}", json=body or {"timer_mode": "with_timer"}, headers=_hdr(owner))
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()


def act(client, token, owner, path, body=None, expect=200):
    res = client.post(f"/api/demo/{token}{path}", json=body or {}, headers=_hdr(owner))
    assert res.status_code == expect, res.text
    return res.json()


def state(client, token, user):
    return client.get(f"/api/play/{token}", headers=_hdr(user)).json()


def answer(client, token, user, text):
    st = state(client, token, user)
    q = st["question"]
    res = client.post(f"/api/play/{token}/answer", headers=_hdr(user),
                      json={"answer": text, "question_id": q["question_id"], "round_question_id": q["round_question_id"]})
    assert res.status_code == 200, res.text
    return res.json()


def attempt_of(db, token):
    db.expire_all()
    return db.query(SoloAttempt).filter_by(session_token=token).one()


# ============================================================================ authorization

def test_anonymous_cannot_use_owner_controls(anon, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    assert anon.post(f"/api/demo/start/{pack.id}", json={}).status_code == status.HTTP_401_UNAUTHORIZED
    for path in ("/skip", "/reveal", "/end", "/restart", "/previous"):
        assert anon.post(f"/api/demo/{token}{path}", json={}).status_code == status.HTTP_401_UNAUTHORIZED
    assert anon.get(f"/api/demo/{token}").status_code == status.HTTP_401_UNAUTHORIZED


def test_ordinary_player_gets_403_everywhere(anon, pack, owner, player):
    token = start_demo(anon, pack, owner)["session_token"]
    assert anon.post(f"/api/demo/start/{pack.id}", json={}, headers=_hdr(player)).status_code == status.HTTP_403_FORBIDDEN
    for path, body in (("/skip", {}), ("/reveal", {}), ("/score", {"action": "add", "delta": 50}),
                       ("/timer", {"action": "expire"}), ("/jump", {"round_index": 0, "question_index": 2}), ("/end", {})):
        assert anon.post(f"/api/demo/{token}{path}", json=body, headers=_hdr(player)).status_code == status.HTTP_403_FORBIDDEN
    # nor can they read the owner's demo through the ordinary game API
    assert anon.get(f"/api/play/{token}", headers=_hdr(player)).status_code == status.HTTP_403_FORBIDDEN


def test_fails_closed_without_owner_configuration(anon, pack, owner, monkeypatch):
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    assert anon.post(f"/api/demo/start/{pack.id}", json={}, headers=_hdr(owner)).status_code == status.HTTP_403_FORBIDDEN


def test_client_side_flags_cannot_create_a_demo_or_unlock_controls(anon, pack, player):
    # an ordinary start with every "demo-ish" field set is still an ordinary ranked game
    res = anon.post(f"/api/play/start/{pack.id}", headers=_hdr(player),
                    json={"timer_mode": "preview", "is_demo": True, "demo": True, "play_mode": "preview"})
    assert res.status_code == status.HTTP_201_CREATED
    token = res.json()["session_token"]
    body = anon.get(f"/api/play/{token}", headers=_hdr(player)).json()
    assert body["ranked"] is True
    # the player's own session is not a demo, so the owner router refuses it even for its owner
    assert anon.post(f"/api/demo/{token}/skip", json={}, headers=_hdr(player)).status_code == status.HTTP_403_FORBIDDEN


def test_owner_cannot_touch_other_players_or_ranked_sessions(anon, db_session, pack, owner, player):
    player_token = _start(anon, pack, player)
    before = attempt_of(db_session, player_token)
    snapshot = (before.current_question_index, before.total_score, before.status)
    for path, body in (("/skip", {}), ("/score", {"action": "add", "delta": 9}), ("/end", {}),
                       ("/timer", {"action": "expire"}), ("/reveal", {})):
        assert anon.post(f"/api/demo/{player_token}{path}", json=body, headers=_hdr(owner)).status_code == status.HTTP_404_NOT_FOUND
    after = attempt_of(db_session, player_token)
    assert (after.current_question_index, after.total_score, after.status) == snapshot
    # the owner's own ordinary (ranked) game is off limits too
    own_ranked = _start(anon, make_classic_pack(db_session, "Egasi reyting"), owner)
    assert anon.post(f"/api/demo/{own_ranked}/skip", json={}, headers=_hdr(owner)).status_code == status.HTTP_404_NOT_FOUND


def test_staff_cannot_open_the_owners_demo(anon, db_session, pack, owner, monkeypatch):
    admin = _user(db_session, "admin-demo@zakowhat.uz", "Admin")
    monkeypatch.setenv("ADMIN_EMAILS", admin.email)
    token = start_demo(anon, pack, owner)["session_token"]
    assert anon.get(f"/api/play/{token}", headers=_hdr(admin)).status_code == status.HTTP_403_FORBIDDEN


def test_me_reports_owner_flag_only_for_owner(anon, owner, player):
    assert anon.get("/api/auth/me", headers=_hdr(owner)).json()["user"]["is_owner"] is True
    assert anon.get("/api/auth/me", headers=_hdr(player)).json()["user"]["is_owner"] is False


# ============================================================================ gameplay — Classic Zakovat

def test_demo_start_is_classified_as_demo(anon, db_session, pack, owner):
    data = start_demo(anon, pack, owner)
    assert data["demo"] is True and data["ranked"] is False
    att = attempt_of(db_session, data["session_token"])
    assert att.timer_mode == "preview" and att.attempt_metadata["is_demo"] is True
    assert att.attempt_metadata["competitive"] is False and is_ranked_or_legacy_timed(att) is False


def test_skip_and_previous_move_without_recording_answers(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    s = act(anon, token, owner, "/skip")
    assert s["position"] == {"round_index": 0, "question_index": 1} and s["question_open"] is True
    assert state(anon, token, owner)["question"]["sequence"] == 2
    s = act(anon, token, owner, "/previous")
    assert s["position"]["question_index"] == 0
    assert db_session.query(AnswerRecord).count() == 0
    # skipping past the last question ends the demo
    act(anon, token, owner, "/jump", {"round_index": 0, "question_index": CLASSIC_Q - 1})
    assert act(anon, token, owner, "/skip")["status"] == "completed"


def test_jump_then_replay_keeps_totals_consistent(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    act(anon, token, owner, "/jump", {"round_index": 0, "question_index": 3})
    assert state(anon, token, owner)["question"]["sequence"] == 4
    res = answer(anon, token, owner, "Javob 4")
    assert res["is_correct"] is True
    assert attempt_of(db_session, token).total_score == 1
    # replay the same question: its answer is removed and the score recomputed
    s = act(anon, token, owner, "/jump", {"round_index": 0, "question_index": 3})
    assert s["score"]["total_correct"] == 0 and s["question_open"] is True
    assert db_session.query(AnswerRecord).count() == 0
    with pytest.raises(AssertionError):
        act(anon, token, owner, "/jump", {"round_index": 0, "question_index": 99})


def test_reveal_never_submits_an_answer(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    before = state(anon, token, owner)["question"]
    r = act(anon, token, owner, "/reveal")
    assert r["correct_answer"] == "Javob 1"
    assert db_session.query(AnswerRecord).count() == 0
    after = state(anon, token, owner)
    assert after["question"]["question_id"] == before["question_id"] and after["total_score"] == 0


def test_timer_controls_move_the_server_clock(anon, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    limit = state(anon, token, owner)["question"]["time_limit_seconds"]
    paused = act(anon, token, owner, "/timer", {"action": "pause"})
    assert paused["timer"]["paused"] is True
    frozen = paused["timer"]["remaining_seconds"]
    time.sleep(1.2)
    q = state(anon, token, owner)["question"]
    assert q["timer_paused"] is True and q["remaining_time_seconds"] == frozen      # time stands still
    assert act(anon, token, owner, "/timer", {"action": "resume"})["timer"]["paused"] is False
    s = act(anon, token, owner, "/timer", {"action": "add", "seconds": -30})
    assert s["timer"]["remaining_seconds"] <= limit - 29
    s = act(anon, token, owner, "/timer", {"action": "add", "seconds": 20})
    assert s["timer"]["remaining_seconds"] >= limit - 11
    assert act(anon, token, owner, "/timer", {"action": "reset"})["timer"]["remaining_seconds"] >= limit - 1
    assert act(anon, token, owner, "/timer", {"action": "expire"})["timer"]["remaining_seconds"] == 0
    assert state(anon, token, owner)["question"]["remaining_time_seconds"] == 0
    act(anon, token, owner, "/timer", {"action": "bogus"}, expect=400)


def test_expired_question_times_out_once_through_the_normal_path(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    act(anon, token, owner, "/timer", {"action": "expire"})
    res = answer(anon, token, owner, "")                        # what the page sends on time-out
    assert res["is_correct"] is False and res["points_awarded"] == 0
    assert db_session.query(AnswerRecord).count() == 1


def test_manual_score_is_flagged_and_resettable(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    answer(anon, token, owner, "Javob 1")
    s = act(anon, token, owner, "/score", {"action": "add", "delta": 3})
    assert s["score"]["total_correct"] == 4 and s["score"]["manual_adjustment"] == 3 and s["score"]["modified"] is True
    s = act(anon, token, owner, "/score", {"action": "add", "delta": -10})
    assert s["score"]["total_correct"] == 0                    # never below zero for correct answers
    s = act(anon, token, owner, "/score", {"action": "reset"})
    assert s["score"]["total_correct"] == 0 and s["score"]["total_score"] == 0 and s["score"]["modified"] is True


def test_restart_and_end(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    answer(anon, token, owner, "Javob 1")
    act(anon, token, owner, "/score", {"action": "add", "delta": 2})
    s = act(anon, token, owner, "/restart")
    assert s["position"] == {"round_index": 0, "question_index": 0} and s["score"]["total_correct"] == 0
    assert s["score"]["modified"] is False and db_session.query(AnswerRecord).count() == 0
    assert act(anon, token, owner, "/end")["status"] == "completed"
    results = anon.get(f"/api/play/{token}/results", headers=_hdr(owner))
    assert results.status_code == 200
    act(anon, token, owner, "/restart")                         # a finished demo can start over
    assert attempt_of(db_session, token).status == "in_progress"


# ============================================================================ gameplay — Svoya Igra

def test_svoya_igra_demo_uses_the_board(anon, db_session, owner):
    quiz = create_svoyak_test_game(anon, db_session, "Namoyish Svoyak")
    data = start_demo(anon, quiz, owner)
    token = data["session_token"]
    assert data["status"] == "board_selection"
    act(anon, token, owner, "/previous", expect=400)            # no order in Svoya Igra
    s = act(anon, token, owner, "/jump", {"round_index": 2, "question_index": 4})
    assert s["question_open"] is True
    correct = act(anon, token, owner, "/reveal")["correct_answer"]
    res = answer(anon, token, owner, correct)
    assert res["is_correct"] is True and res["points_awarded"] == 50
    # skip from an open cell returns to the board and leaves that cell unplayed
    act(anon, token, owner, "/jump", {"round_index": 0, "question_index": 0})
    s = act(anon, token, owner, "/skip")
    assert s["status"] == "board_selection"
    board = anon.get(f"/api/play/{token}/board", headers=_hdr(owner)).json()["board"]
    assert board["themes"][0]["cells"][0]["is_played"] is False and board["themes"][2]["cells"][4]["is_played"] is True
    s = act(anon, token, owner, "/score", {"action": "add", "delta": -20})
    assert s["score"]["total_score"] == 30


# ============================================================================ data integrity

def _complete_demo_with_inflated_score(client, quiz, owner):
    token = start_demo(client, quiz, owner)["session_token"]
    for i in range(1, CLASSIC_Q + 1):
        answer(client, token, owner, f"Javob {i}")
        client.post(f"/api/play/{token}/continue", headers=_hdr(owner))
    act(client, token, owner, "/score", {"action": "add", "delta": 500})
    act(client, token, owner, "/end")
    return token


def test_demo_never_reaches_leaderboards_stats_progress_or_daily(anon, db_session, pack, owner):
    _complete_demo_with_inflated_score(anon, pack, owner)
    board = anon.get(f"/api/quizzes/{pack.id}/leaderboard").json()
    assert board["leaderboard"] == [] and board["total_participants"] == 0
    assert anon.get(f"/api/quizzes/{pack.id}").json()["top_player"] is None
    listed = next(q for q in anon.get("/api/quizzes").json() if q["quiz_id"] == pack.id)
    assert listed["top_player"] is None
    stats = anon.get("/api/auth/profile", headers=_hdr(owner)).json()["stats"]["by_format"]
    assert stats["classic"]["games"] == 0 and stats["classic"]["correct"] == 0 and stats["practice_games"] == 0
    assert str(pack.id) not in anon.get("/api/me/pack-progress", headers=_hdr(owner)).json()["packs"]
    assert anon.get("/api/me/active-game", headers=_hdr(owner)).json()["game"] is None
    assert db_session.query(DailySubmission).count() == 0


def test_ranked_play_unchanged_next_to_a_demo(anon, db_session, pack, owner, player):
    _complete_demo_with_inflated_score(anon, pack, owner)
    token = _start(anon, pack, player)
    for i in range(1, CLASSIC_Q + 1):
        answer(anon, token, player, f"Javob {i}" if i != 2 else "noto‘g‘ri")
        anon.post(f"/api/play/{token}/continue", headers=_hdr(player))
    rows = anon.get(f"/api/quizzes/{pack.id}/leaderboard").json()["leaderboard"]
    assert len(rows) == 1 and rows[0]["correct_count"] == CLASSIC_Q - 1 and rows[0]["display_name"] == "Oddiy O‘yinchi"


def test_manual_adjustment_survives_normal_completion_and_shows_in_results(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    act(anon, token, owner, "/score", {"action": "add", "delta": 2})
    for i in range(1, CLASSIC_Q + 1):                 # finish through the ordinary answer/continue path
        answer(anon, token, owner, f"Javob {i}" if i <= 3 else "xato")
        anon.post(f"/api/play/{token}/continue", headers=_hdr(owner))
    att = attempt_of(db_session, token)
    assert att.status == "completed" and att.total_correct == 3 + 2
    results = anon.get(f"/api/play/{token}/results", headers=_hdr(owner)).json()
    assert results["total_correct"] == 5
    # and still nothing on the public board
    assert anon.get(f"/api/quizzes/{pack.id}/leaderboard").json()["leaderboard"] == []


def test_a_normal_start_never_resumes_a_demo(anon, db_session, pack, owner):
    demo_token = start_demo(anon, pack, owner)["session_token"]
    normal = anon.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=_hdr(owner))
    assert normal.status_code == status.HTTP_201_CREATED
    data = normal.json()
    assert data["session_token"] != demo_token
    # the owner has seen the answers in the demo, so their own game on this pack is practice, never ranked
    assert data["ranked"] is False


# ============================================================================ header "Qaytadan boshlash" on a demo

def header_restart(client, token, user):
    res = client.post(f"/api/play/{token}/restart", json={}, headers=_hdr(user))
    assert res.status_code == 200, res.text
    return res.json()


def assert_clean_owner_demo(client, db, token, owner):
    att = attempt_of(db, token)
    meta = att.attempt_metadata
    assert att.timer_mode == "preview" and meta["is_demo"] is True and meta["competitive"] is False
    assert is_ranked_or_legacy_timed(att) is False
    for key in ("demo_score_adjustment", "demo_score_modified", "demo_paused_at", "awaiting_continue"):
        assert key not in meta, key
    assert att.total_score == 0 and att.total_correct == 0 and att.completed_at is None
    assert db.query(AnswerRecord).filter(AnswerRecord.attempt_id == att.id).count() == 0
    s = client.get(f"/api/demo/{token}", headers=_hdr(owner)).json()      # owner controls still attached
    assert s["score"] == {"total_score": 0, "total_correct": 0, "manual_adjustment": 0, "modified": False}
    return att


def test_header_restart_on_active_demo_clears_manual_adjustment(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    answer(anon, token, owner, "Javob 1")
    anon.post(f"/api/play/{token}/continue", headers=_hdr(owner))
    act(anon, token, owner, "/score", {"action": "add", "delta": 3})
    data = header_restart(anon, token, owner)
    assert data["session_token"] == token and data["ranked"] is False       # same session, in place
    att = assert_clean_owner_demo(anon, db_session, token, owner)
    assert (att.current_round_index, att.current_question_index, att.status) == (0, 0, "in_progress")
    # finishing with all-wrong answers now gives 0, not the old adjustment (the audit's P3 path)
    for _ in range(CLASSIC_Q):
        answer(anon, token, owner, "xato")
        anon.post(f"/api/play/{token}/continue", headers=_hdr(owner))
    assert attempt_of(db_session, token).total_correct == 0


def test_header_restart_on_paused_demo_resets_the_clock(anon, db_session, pack, owner):
    token = start_demo(anon, pack, owner)["session_token"]
    act(anon, token, owner, "/skip")
    act(anon, token, owner, "/timer", {"action": "pause"})
    time.sleep(1.1)
    data = header_restart(anon, token, owner)
    q = data["current_state"]["question"]
    assert q["timer_paused"] is False and q["remaining_time_seconds"] >= q["time_limit_seconds"] - 1
    assert_clean_owner_demo(anon, db_session, token, owner)
    st = anon.get(f"/api/demo/{token}", headers=_hdr(owner)).json()
    assert st["timer"]["paused"] is False and st["position"] == {"round_index": 0, "question_index": 0}


def test_header_restart_on_completed_demo_reuses_the_demo(anon, db_session, pack, owner, monkeypatch):
    token = start_demo(anon, pack, owner)["session_token"]
    act(anon, token, owner, "/score", {"action": "add", "delta": 2})
    act(anon, token, owner, "/end")
    attempts_before = db_session.query(SoloAttempt).count()
    data = header_restart(anon, token, owner)
    assert data["session_token"] == token and data["ranked"] is False
    assert db_session.query(SoloAttempt).count() == attempts_before          # no new (non-demo) attempt
    assert_clean_owner_demo(anon, db_session, token, owner)


def test_restarted_demo_stays_owner_only_and_out_of_competitive_data(anon, db_session, owner, player, monkeypatch):
    quiz = create_svoyak_test_game(anon, db_session, "Namoyish restart")
    token = start_demo(anon, quiz, owner)["session_token"]
    act(anon, token, owner, "/jump", {"round_index": 0, "question_index": 4})
    answer(anon, token, owner, act(anon, token, owner, "/reveal")["correct_answer"])
    act(anon, token, owner, "/score", {"action": "add", "delta": 100})
    act(anon, token, owner, "/end")
    data = header_restart(anon, token, owner)
    assert data["status"] == "board_selection" and data["svoyak_mode"] == "free_choice"
    assert_clean_owner_demo(anon, db_session, token, owner)
    # owner-only: staff and players are refused on both the game API and the controls
    admin = _user(db_session, "admin-restart@zakowhat.uz", "Admin")
    monkeypatch.setenv("ADMIN_EMAILS", admin.email)
    for user in (admin, player):
        assert anon.get(f"/api/play/{token}", headers=_hdr(user)).status_code == status.HTTP_403_FORBIDDEN
        assert anon.post(f"/api/play/{token}/restart", json={}, headers=_hdr(user)).status_code == status.HTTP_403_FORBIDDEN
        assert anon.get(f"/api/demo/{token}", headers=_hdr(user)).status_code == status.HTTP_403_FORBIDDEN
    # play it to the end again: still nowhere in competitive data
    act(anon, token, owner, "/jump", {"round_index": 1, "question_index": 4})
    answer(anon, token, owner, act(anon, token, owner, "/reveal")["correct_answer"])
    act(anon, token, owner, "/end")
    assert anon.get(f"/api/quizzes/{quiz.id}/leaderboard").json()["leaderboard"] == []
    stats = anon.get("/api/auth/profile", headers=_hdr(owner)).json()["stats"]["by_format"]
    assert stats["svoyak"]["games"] == 0 and stats["practice_games"] == 0
    assert str(quiz.id) not in anon.get("/api/me/pack-progress", headers=_hdr(owner)).json()["packs"]


def test_ordinary_restart_behaviour_unchanged(anon, db_session, pack, player):
    """Ranked: abandoned + a new practice attempt. Practice: reset in place. (Regression guard.)"""
    ranked = _start(anon, pack, player)
    data = header_restart(anon, ranked, player)
    assert data["session_token"] != ranked and data["ranked"] is False
    assert attempt_of(db_session, ranked).status == "abandoned"
    practice = data["session_token"]
    again = header_restart(anon, practice, player)
    assert again["session_token"] == practice and again["ranked"] is False
