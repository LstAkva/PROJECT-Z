"""
Phase 7 / 9 — player progress through real games: per-format statistics, Arena pack progress and the
resumable game. Everything plays through the public API against the test database.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status
from sqlalchemy import event

from api.auth import create_access_token, hash_password
from models import AcceptedAnswer, Question, Quiz, QuizVersion, Round, RoundQuestion, SoloAttempt, User
from services.player_progress import pack_progress, resumable_game
from tests.conftest import TestingSessionLocal, test_engine
from tests.test_svoyak_gameplay import create_svoyak_test_game

CLASSIC_Q = 6   # a short single-round Classic pack keeps the tests fast; the logic is size-independent


def make_classic_pack(db, title="Progress Classic"):
    quiz = Quiz(title=title, description="progress")
    db.add(quiz)
    db.flush()
    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="classic_zakovat")
    db.add(version)
    db.flush()
    rnd = Round(quiz_version_id=version.id, sequence=1, round_type="zakovat_classic", config={"name": "1-tur"})
    db.add(rnd)
    db.flush()
    manifest = []
    for i in range(1, CLASSIC_Q + 1):
        q = Question(text=f"{title} savol {i}", points=1, status="approved")
        db.add(q)
        db.flush()
        a = AcceptedAnswer(question_id=q.id, answer_text=f"Javob {i}", is_primary=True)
        db.add(a)
        rq = RoundQuestion(round_id=rnd.id, question_id=q.id, sequence=i)
        db.add(rq)
        db.flush()
        manifest.append({"question_id": q.id, "round_question_id": rq.id, "sequence": i, "text": q.text,
                         "primary_answer": a.answer_text, "points": 1,
                         "accepted_answers": [{"answer_text": a.answer_text, "is_primary": True}]})
    version.published_manifest = {"title": title, "game_mode": "classic_zakovat", "category": "Tarix",
                                  "rounds": [{"round_id": rnd.id, "sequence": 1, "round_type": "zakovat_classic",
                                              "config": {"name": "1-tur"}, "questions": manifest}]}
    db.commit()
    return quiz


@pytest.fixture
def player(db_session):
    return _user(db_session, "progress_player@zakowhat.uz", "Progress O‘yinchi")


def _user(db, email, name):
    user = User(email=email, display_name=name, hashed_password=hash_password("Pass123!"), auth_provider="local")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hdr(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


def _start(client, quiz, user, **body):
    res = client.post(f"/api/play/start/{quiz.id}", json=body or {"timer_mode": "with_timer"}, headers=_hdr(user))
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()["session_token"]


def _answer_classic(client, token, user, n, wrong=()):
    """Answers the next n questions of a Classic attempt (numbers in `wrong` get a wrong answer)."""
    for _ in range(n):
        state = client.get(f"/api/play/{token}", headers=_hdr(user)).json()
        seq = state["current_state"]["question"]["sequence"] if state.get("current_state") else state["question"]["sequence"]
        answer = "xato" if seq in wrong else f"Javob {seq}"
        res = client.post(f"/api/play/{token}/answer", json={"answer": answer}, headers=_hdr(user))
        assert res.status_code == status.HTTP_200_OK, res.text
        if res.json().get("awaiting_continue"):
            client.post(f"/api/play/{token}/continue", headers=_hdr(user))


def _finish(client, token, user):
    for _ in range(4):
        if client.get(f"/api/play/{token}", headers=_hdr(user)).json().get("status") == "completed":
            return
        client.post(f"/api/play/{token}/continue", headers=_hdr(user))
    assert client.get(f"/api/play/{token}", headers=_hdr(user)).json().get("status") == "completed"


def _progress(client, user):
    res = client.get("/api/me/pack-progress", headers=_hdr(user))
    assert res.status_code == status.HTTP_200_OK, res.text
    return res.json()["packs"]


# =========================================================================== Arena pack progress

def test_untouched_packs_have_no_progress(client, db_session, player):
    make_classic_pack(db_session)
    assert _progress(client, player) == {}


def test_progress_counts_distinct_questions_in_the_current_attempt(client, db_session, player):
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    _answer_classic(client, token, player, 3, wrong={2})
    p = _progress(client, player)[str(quiz.id)]
    assert (p["state"], p["format"], p["answered"], p["total"]) == ("in_progress", "classic", 3, CLASSIC_Q)
    assert p["completed_before"] is False and p["best"] is None


def test_completed_pack_reports_the_best_result_in_its_own_scoring(client, db_session, player):
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    _answer_classic(client, token, player, CLASSIC_Q, wrong={1, 4})
    _finish(client, token, player)
    p = _progress(client, player)[str(quiz.id)]
    assert p == {"state": "completed", "format": "classic", "best": {"correct": CLASSIC_Q - 2}, "completed_count": 1}


def test_a_restart_is_a_new_attempt_and_answers_never_add_up(client, db_session, player):
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    _answer_classic(client, token, player, CLASSIC_Q)
    _finish(client, token, player)
    new = client.post(f"/api/play/{token}/restart", headers=_hdr(player))
    assert new.status_code == status.HTTP_200_OK, new.text
    _answer_classic(client, new.json()["session_token"], player, 2)
    p = _progress(client, player)[str(quiz.id)]
    assert (p["state"], p["answered"], p["total"]) == ("in_progress", 2, CLASSIC_Q)   # not 6 + 2
    assert p["completed_before"] is True and p["best"] == {"correct": CLASSIC_Q}


def test_svoyak_progress_and_best_points(client, db_session, player):
    quiz = create_svoyak_test_game(client, db_session, "Progress Svoyak")
    token = _start(client, quiz, player, svoyak_mode="free_choice")
    for r_idx, q_idx, answer in ((0, 0, "Javob 1_10"), (1, 1, "xato"), (2, 4, "Javob 3_50")):
        client.post(f"/api/play/{token}/select", json={"round_index": r_idx, "question_index": q_idx}, headers=_hdr(player))
        res = client.post(f"/api/play/{token}/answer", json={"answer": answer}, headers=_hdr(player))
        if res.json().get("awaiting_continue"):
            client.post(f"/api/play/{token}/continue", headers=_hdr(player))
    p = _progress(client, player)[str(quiz.id)]
    assert (p["state"], p["format"], p["answered"], p["total"]) == ("in_progress", "svoyak", 3, 25)


def test_an_inactive_game_is_unfinished_not_in_progress_and_is_not_touched(client, db_session, player):
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    _answer_classic(client, token, player, 1)
    s = TestingSessionLocal()
    attempt = s.query(SoloAttempt).filter_by(session_token=token).one()
    attempt.attempt_metadata = {**(attempt.attempt_metadata or {}),
                                "last_active_at": (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()}
    s.commit()
    s.close()
    assert _progress(client, player)[str(quiz.id)] == {"state": "unfinished", "format": "classic"}
    assert client.get("/api/me/active-game", headers=_hdr(player)).json()["game"] is None
    s = TestingSessionLocal()
    assert s.query(SoloAttempt).filter_by(session_token=token).one().status == "in_progress"   # read-only
    s.close()


def test_progress_is_private_to_each_player(client, db_session, player):
    quiz = make_classic_pack(db_session)
    _answer_classic(client, _start(client, quiz, player), player, 2)
    other = _user(db_session, "progress_other@zakowhat.uz", "Boshqa")
    assert _progress(client, other) == {}
    assert client.get("/api/me/active-game", headers=_hdr(other)).json()["game"] is None
    client.cookies.clear()
    assert client.get("/api/me/pack-progress").status_code == status.HTTP_401_UNAUTHORIZED
    anonymous = client.get("/api/me/active-game")                     # homepage asks on every visit
    assert anonymous.status_code == status.HTTP_200_OK and anonymous.json() == {"game": None}
    forged = client.get("/api/me/active-game", headers={"Authorization": "Bearer not-a-token"})
    assert forged.json() == {"game": None}


def test_preview_attempts_are_ignored(client, db_session, player):
    quiz = make_classic_pack(db_session)
    version = db_session.query(QuizVersion).filter_by(quiz_id=quiz.id).one()
    db_session.add(SoloAttempt(quiz_version_id=version.id, user_id=player.id, timer_mode="preview",
                               status="completed", total_correct=6))
    db_session.commit()
    assert _progress(client, player) == {}
    stats = client.get("/api/auth/profile", headers=_hdr(player)).json()["stats"]["by_format"]
    assert stats["classic"]["games"] == 0


def test_pack_progress_query_count_does_not_grow_with_packs(db_session, player, client):
    """No N+1: the number of SQL statements is the same for one pack or five."""
    def count_queries(user_id):
        statements = []
        listener = lambda *args: statements.append(1)     # noqa: E731
        event.listen(test_engine, "before_cursor_execute", listener)
        try:
            s = TestingSessionLocal()
            pack_progress(s, user_id)
            s.close()
        finally:
            event.remove(test_engine, "before_cursor_execute", listener)
        return len(statements)

    quizzes = [make_classic_pack(db_session, f"N1 Pack {i}") for i in range(5)]
    _answer_classic(client, _start(client, quizzes[0], player), player, 1)
    one = count_queries(player.id)
    for q in quizzes[1:]:
        _answer_classic(client, _start(client, q, player), player, 1)
    five = count_queries(player.id)
    assert one == five <= 3, (one, five)


# =========================================================================== resumable game

def test_active_game_points_to_the_existing_attempt(client, db_session, player):
    classic = make_classic_pack(db_session)
    token = _start(client, classic, player)
    _answer_classic(client, token, player, 2)
    game = client.get("/api/me/active-game", headers=_hdr(player)).json()["game"]
    assert game["session_token"] == token and game["quiz_id"] == classic.id
    assert (game["format"], game["answered"], game["total"], game["title"]) == ("classic", 2, CLASSIC_Q, "Progress Classic")
    # the client resumes through the existing state endpoint: same attempt, same next question
    state = client.get(f"/api/play/{token}", headers=_hdr(player)).json()
    seq = state["current_state"]["question"]["sequence"] if state.get("current_state") else state["question"]["sequence"]
    assert seq == 3


def test_active_game_can_be_filtered_by_pack_and_prefers_the_latest(client, db_session, player):
    a = make_classic_pack(db_session, "Birinchi")
    b = make_classic_pack(db_session, "Ikkinchi")
    _answer_classic(client, _start(client, a, player), player, 1)
    token_b = _start(client, b, player)
    assert client.get("/api/me/active-game", headers=_hdr(player)).json()["game"]["session_token"] == token_b
    assert client.get(f"/api/me/active-game?quiz_id={a.id}", headers=_hdr(player)).json()["game"]["quiz_id"] == a.id
    assert client.get("/api/me/active-game?quiz_id=999999", headers=_hdr(player)).json()["game"] is None


def test_completed_games_are_not_offered_for_resumption(client, db_session, player):
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    _answer_classic(client, token, player, CLASSIC_Q)
    _finish(client, token, player)
    assert client.get("/api/me/active-game", headers=_hdr(player)).json()["game"] is None


def test_resuming_never_repeats_or_rescores_a_question(client, db_session, player):
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    _answer_classic(client, token, player, 2)
    before = client.get(f"/api/play/{token}", headers=_hdr(player)).json()
    # reopening the pack from the Arena (what "Davom etish" does) returns the same attempt
    again = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "with_timer"}, headers=_hdr(player))
    assert again.json()["session_token"] == token and again.json().get("resumed") is True
    after = client.get(f"/api/play/{token}", headers=_hdr(player)).json()
    assert after["total_score"] == before["total_score"]
    s = TestingSessionLocal()
    attempt = s.query(SoloAttempt).filter_by(session_token=token).one()
    assert len(attempt.answers) == 2 and s.query(SoloAttempt).filter_by(user_id=player.id).count() == 1
    s.close()


def test_resumed_timer_continues_from_the_server_clock(client, db_session, player):
    """A reload never grants new time: remaining seconds come from when the question was opened."""
    quiz = make_classic_pack(db_session)
    token = _start(client, quiz, player)
    s = TestingSessionLocal()
    attempt = s.query(SoloAttempt).filter_by(session_token=token).one()
    attempt.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=40)
    s.commit()
    s.close()
    q = client.get(f"/api/play/{token}", headers=_hdr(player)).json()
    q = (q.get("current_state") or q)["question"]
    assert q["time_limit_seconds"] - 42 <= q["remaining_time_seconds"] <= q["time_limit_seconds"] - 39


# =========================================================================== profile statistics

def test_profile_statistics_never_mix_formats(client, db_session, player):
    classic = make_classic_pack(db_session)
    token = _start(client, classic, player)
    _answer_classic(client, token, player, CLASSIC_Q, wrong={3})
    _finish(client, token, player)

    svoyak = create_svoyak_test_game(client, db_session, "Stats Svoyak")
    st = _start(client, svoyak, player, svoyak_mode="free_choice")
    for r in range(5):
        for qi, pts in enumerate((10, 20, 30, 40, 50)):
            client.post(f"/api/play/{st}/select", json={"round_index": r, "question_index": qi}, headers=_hdr(player))
            ans = "xato" if (r, qi) == (0, 4) else f"Javob {r + 1}_{pts}"
            res = client.post(f"/api/play/{st}/answer", json={"answer": ans}, headers=_hdr(player))
            if res.json().get("awaiting_continue"):
                client.post(f"/api/play/{st}/continue", headers=_hdr(player))
    _finish(client, st, player)

    by = client.get("/api/auth/profile", headers=_hdr(player)).json()["stats"]["by_format"]
    assert by["classic"] == {"games": 1, "correct": CLASSIC_Q - 1, "answered": CLASSIC_Q,
                             "accuracy_percent": round((CLASSIC_Q - 1) / CLASSIC_Q * 100, 1)}
    assert by["svoyak"]["games"] == 1 and by["svoyak"]["correct"] == 24 and by["svoyak"]["answered"] == 25
    assert by["svoyak"]["total_points"] == by["svoyak"]["best_points"] == 750 - 50 - 50
    assert "total_points" not in by["classic"]
