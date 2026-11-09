"""
Ranked-attempt rule (leaderboard exploit fix).

Approved rules:
1. One ranked opportunity per user per PACK (across versions).
2. No-timer runs are unranked practice; no-timer only unlocks after the timed ranked attempt was started (option B).
3. The first started timed attempt is the ranked one; restarting never resets or recreates it; later attempts are practice.
4. Refresh/leave/return resumes the same attempt; explicit restart consumes the opportunity; expired attempts are
   never revived as ranked.
5. Historical (pre-rule) results keep their existing leaderboard treatment.
"""
from datetime import datetime, timezone

import pytest
from fastapi import status

from api.auth import create_access_token, hash_password
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, AnswerRecord, SoloAttempt, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(db_session, email, name="Ranked Player"):
    user = User(email=email, display_name=name, hashed_password=hash_password("Pass123!"), auth_provider="local")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


def _add_version(db_session, quiz, version_number):
    """Published 1-round, 2-question standard pack version (non-paced: completes via /continue)."""
    version = QuizVersion(quiz_id=quiz.id, version_number=version_number, status="published", game_mode="modern_multiround")
    db_session.add(version)
    db_session.flush()
    rnd = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db_session.add(rnd)
    db_session.flush()
    qs = []
    for i in (1, 2):
        q = Question(text=f"Ranked savol {i} v{version_number}", points=1, status="approved")
        db_session.add(q)
        db_session.flush()
        a = AcceptedAnswer(question_id=q.id, answer_text=f"A{i}", is_primary=True)
        db_session.add(a)
        rq = RoundQuestion(round_id=rnd.id, question_id=q.id, sequence=i)
        db_session.add(rq)
        db_session.flush()
        qs.append({"question_id": q.id, "round_question_id": rq.id, "sequence": i, "text": q.text, "points": 1,
                   "primary_answer": "A" + str(i), "accepted_answers": [{"answer_text": f"A{i}", "is_primary": True}]})
    version.published_manifest = {"title": quiz.title, "game_mode": "modern_multiround",
                                  "rounds": [{"round_id": rnd.id, "sequence": 1, "round_type": "standard", "questions": qs}]}
    db_session.commit()
    return version


@pytest.fixture
def pack(db_session):
    quiz = Quiz(title="Ranked Rule Pack", description="2 savol")
    db_session.add(quiz)
    db_session.flush()
    _add_version(db_session, quiz, 1)
    return quiz


def _start(client, quiz, headers, **body):
    return client.post(f"/api/play/start/{quiz.id}", json=body, headers=headers)


def _play_to_completion(client, token, headers, answers):
    for ans in answers:
        assert client.post(f"/api/play/{token}/answer", json={"answer": ans}, headers=headers).status_code == 200
    assert client.post(f"/api/play/{token}/continue", headers=headers).status_code == 200


def _attempt(db_session, token):
    att = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    db_session.refresh(att)
    return att


def _board(client, quiz):
    return client.get(f"/api/quizzes/{quiz.id}/leaderboard").json()["leaderboard"]


# ---------------------------------------------------------------------------
# Rule 3: first timed attempt is ranked; practice never improves it
# ---------------------------------------------------------------------------

def test_first_timed_attempt_is_ranked_and_replay_cannot_improve(client, db_session, pack):
    user = _make_user(db_session, "ranked1@zakowhat.uz", "Ranked One")
    h = _headers(user)

    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    assert _attempt(db_session, t1).attempt_metadata["competitive"] is True
    _play_to_completion(client, t1, h, ["A1", "xato"])  # 1/2

    # Restart from results -> practice, then a perfect run
    t2 = client.post(f"/api/play/{t1}/restart", headers=h).json()["session_token"]
    assert t2 != t1
    assert _attempt(db_session, t2).attempt_metadata["competitive"] is False
    _play_to_completion(client, t2, h, ["A1", "A2"])  # 2/2 practice

    board = _board(client, pack)
    assert len(board) == 1
    assert board[0]["correct_count"] == 1  # ranked result kept; practice ignored
    assert _attempt(db_session, t1).status == "completed"  # historical row intact


def test_restart_mid_game_abandons_ranked_attempt_and_old_token_is_dead(client, db_session, pack):
    """The core exploit: see answers, restart, replay. Restart must never reset the ranked attempt in place."""
    user = _make_user(db_session, "ranked2@zakowhat.uz")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    client.post(f"/api/play/{t1}/answer", json={"answer": "xato"}, headers=h)

    restarted = client.post(f"/api/play/{t1}/restart", headers=h).json()
    t2 = restarted["session_token"]
    assert t2 != t1
    old = _attempt(db_session, t1)
    assert old.status == "abandoned"
    # The abandoned attempt keeps its history (not wiped like the old in-place reset)
    assert db_session.query(AnswerRecord).filter(AnswerRecord.attempt_id == old.id).count() == 1
    assert _attempt(db_session, t2).attempt_metadata["competitive"] is False

    # The abandoned ranked session can no longer be read, answered or continued
    assert client.get(f"/api/play/{t1}", headers=h).status_code == status.HTTP_410_GONE
    assert client.post(f"/api/play/{t1}/answer", json={"answer": "A2"}, headers=h).status_code == status.HTTP_410_GONE
    assert client.post(f"/api/play/{t1}/continue", headers=h).status_code == status.HTTP_410_GONE

    _play_to_completion(client, t2, h, ["A1", "A2"])
    assert _board(client, pack) == []  # ranked attempt was abandoned; perfect practice never ranks


def test_start_with_restart_flag_cannot_bypass(client, db_session, pack):
    """restart=true on /start creates a practice attempt and ends the active ranked one."""
    user = _make_user(db_session, "ranked3@zakowhat.uz")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    client.post(f"/api/play/{t1}/answer", json={"answer": "A1"}, headers=h)

    t2 = _start(client, pack, h, timer_mode="with_timer", restart=True).json()["session_token"]
    assert t2 != t1
    assert _attempt(db_session, t2).attempt_metadata["competitive"] is False
    assert _attempt(db_session, t1).status == "abandoned"
    # Learn answers in practice, then try to go back and finish the ranked attempt -> blocked
    _play_to_completion(client, t2, h, ["A1", "A2"])
    assert client.post(f"/api/play/{t1}/answer", json={"answer": "A2"}, headers=h).status_code == status.HTTP_410_GONE
    assert _board(client, pack) == []


def test_restart_endpoint_twice_never_recreates_ranked_attempt(client, db_session, pack):
    user = _make_user(db_session, "ranked4@zakowhat.uz")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    t2 = client.post(f"/api/play/{t1}/restart", headers=h).json()["session_token"]
    t3 = client.post(f"/api/play/{t1}/restart", headers=h).json()["session_token"]  # restart the abandoned one again
    assert len({t1, t2, t3}) == 3
    ranked = [a for a in db_session.query(SoloAttempt).filter(SoloAttempt.user_id == user.id).all()
              if (a.attempt_metadata or {}).get("competitive") is True]
    assert [a.session_token for a in ranked] == [t1]


# ---------------------------------------------------------------------------
# Rule 2 (option B): no-timer locked until the timed ranked attempt is started; never ranked
# ---------------------------------------------------------------------------

def test_no_timer_is_locked_before_first_timed_attempt(client, db_session, pack):
    user = _make_user(db_session, "ranked5@zakowhat.uz")
    h = _headers(user)
    res = _start(client, pack, h, timer_mode="no_timer")
    assert res.status_code == status.HTTP_409_CONFLICT
    assert "Vaqtsiz rejim" in res.json()["detail"]
    assert db_session.query(SoloAttempt).filter(SoloAttempt.user_id == user.id).count() == 0


def test_no_timer_practice_after_ranked_never_enters_leaderboard(client, db_session, pack):
    user = _make_user(db_session, "ranked6@zakowhat.uz", "No Timer Player")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    _play_to_completion(client, t1, h, ["xato", "xato"])  # 0/2 ranked

    res = _start(client, pack, h, timer_mode="no_timer")
    assert res.status_code == status.HTTP_201_CREATED
    t2 = res.json()["session_token"]
    assert _attempt(db_session, t2).attempt_metadata["competitive"] is False
    _play_to_completion(client, t2, h, ["A1", "A2"])

    board = _board(client, pack)
    assert len(board) == 1 and board[0]["correct_count"] == 0


# ---------------------------------------------------------------------------
# Rule 4: refresh/resume keeps the ranked attempt; expired is never revived as ranked
# ---------------------------------------------------------------------------

def test_refresh_and_rejoin_resume_the_same_ranked_attempt(client, db_session, pack):
    user = _make_user(db_session, "ranked7@zakowhat.uz", "Resumer")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    client.post(f"/api/play/{t1}/answer", json={"answer": "A1"}, headers=h)

    assert client.get(f"/api/play/{t1}", headers=h).status_code == 200          # page refresh
    rejoin = _start(client, pack, h, timer_mode="with_timer").json()             # leave & come back via /play/{id}
    assert rejoin["session_token"] == t1 and rejoin.get("resumed") is True
    att = _attempt(db_session, t1)
    assert att.status == "in_progress" and att.attempt_metadata["competitive"] is True

    client.post(f"/api/play/{t1}/answer", json={"answer": "A2"}, headers=h)
    client.post(f"/api/play/{t1}/continue", headers=h)
    board = _board(client, pack)
    assert len(board) == 1 and board[0]["correct_count"] == 2


def test_expired_ranked_attempt_is_not_revived(client, db_session, pack):
    user = _make_user(db_session, "ranked8@zakowhat.uz")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    att = _attempt(db_session, t1)
    att.status = "expired"
    db_session.commit()

    t2 = client.post(f"/api/play/{t1}/restart", headers=h).json()["session_token"]
    assert t2 != t1
    assert _attempt(db_session, t1).status == "expired"  # not resurrected in place
    assert _attempt(db_session, t2).attempt_metadata["competitive"] is False
    t3 = _start(client, pack, h, timer_mode="with_timer", restart=True).json()["session_token"]
    assert _attempt(db_session, t3).attempt_metadata["competitive"] is False
    _play_to_completion(client, t3, h, ["A1", "A2"])
    assert _board(client, pack) == []


# ---------------------------------------------------------------------------
# Rule 1: per pack across versions
# ---------------------------------------------------------------------------

def test_ranked_opportunity_is_per_pack_across_versions(client, db_session, pack):
    user = _make_user(db_session, "ranked9@zakowhat.uz", "Versioned")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    _play_to_completion(client, t1, h, ["A1", "xato"])

    _add_version(db_session, pack, 2)  # republished pack: new latest version
    t2 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    att2 = _attempt(db_session, t2)
    assert att2.quiz_version.version_number == 2
    assert att2.attempt_metadata["competitive"] is False
    _play_to_completion(client, t2, h, ["A1", "A2"])

    assert _board(client, pack) == []  # v2 board: this user's ranked attempt was on v1
    v1 = client.get(f"/api/quizzes/{pack.id}/versions/1/leaderboard").json()["leaderboard"]
    assert len(v1) == 1 and v1[0]["correct_count"] == 1


# ---------------------------------------------------------------------------
# Rule 5: historical results keep their treatment; legacy players cannot gain a new ranked attempt
# ---------------------------------------------------------------------------

def _legacy_attempt(db_session, user, version, correct, timer_mode="standard"):
    att = SoloAttempt(user_id=user.id, quiz_version_id=version.id, timer_mode=timer_mode, status="completed",
                      total_score=correct, total_correct=correct, active_time_seconds=30,
                      completed_at=datetime.now(timezone.utc), attempt_metadata={"timer_mode": "with_timer"})
    db_session.add(att)
    db_session.commit()
    return att


def test_historical_results_keep_best_attempt_treatment(client, db_session, pack):
    version = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == pack.id).first()
    legacy_user = _make_user(db_session, "legacy@zakowhat.uz", "Legacy Player")
    _legacy_attempt(db_session, legacy_user, version, 1)
    _legacy_attempt(db_session, legacy_user, version, 2)          # pre-rule replay: best-of still applies
    nt_user = _make_user(db_session, "legacynt@zakowhat.uz", "Legacy NoTimer")
    _legacy_attempt(db_session, nt_user, version, 1, timer_mode="no_timer")  # ranked before the rule: preserved

    board = {row["display_name"]: row["correct_count"] for row in _board(client, pack)}
    assert board == {"Legacy Player": 2, "Legacy NoTimer": 1}

    # A legacy player already used the opportunity: new timed runs are practice and change nothing
    h = _headers(legacy_user)
    t = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    assert _attempt(db_session, t).attempt_metadata["competitive"] is False
    _play_to_completion(client, t, h, ["xato", "xato"])
    board_after = {row["display_name"]: row["correct_count"] for row in _board(client, pack)}
    assert board_after == board


def test_concurrent_double_ranked_flags_only_earliest_counts(client, db_session, pack):
    """Defense in depth: if two attempts were ever flagged ranked (race), only the earliest is eligible."""
    user = _make_user(db_session, "race@zakowhat.uz", "Racer")
    h = _headers(user)
    t1 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    _play_to_completion(client, t1, h, ["xato", "xato"])  # 0/2
    t2 = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    _play_to_completion(client, t2, h, ["A1", "A2"])      # 2/2
    att2 = _attempt(db_session, t2)
    meta = dict(att2.attempt_metadata)
    meta["competitive"] = True                             # simulate a race that flagged both
    att2.attempt_metadata = meta
    db_session.commit()

    board = _board(client, pack)
    assert len(board) == 1 and board[0]["correct_count"] == 0
