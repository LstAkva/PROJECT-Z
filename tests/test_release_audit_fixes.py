"""
Release-audit fixes:
- SEC-01 (CRITICAL): public quiz detail never exposes question stems, options, media, answers or explanations
  (both the published-manifest path and the database fallback path).
- SEC-04 (HIGH): reopening a pack during round_reveal / intermediate_break / reveal pause resumes the SAME
  ranked attempt (never abandons it) and never leaks a withheld next question through /start.
- SEC-09 (MEDIUM): timeout policy uses the authoritative competitive flag: ranked and legacy timed runs are
  strict; timed practice runs are forgiving.
"""
from datetime import datetime, timezone, timedelta

import re

import pytest
from fastapi import status

from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt
from tests.test_ranked_attempt_rule import _add_version, _make_user, _headers, _attempt, _board
from tests.test_answer_reveal import create_classic_zakovat


SECRET_TEXT = "MAXFIY SAVOL MATNI 7731"
SECRET_ANSWER = "MAXFIY JAVOB 7731"
SECRET_EXPLANATION = "MAXFIY IZOH 7731"
SECRET_OPTION = "MAXFIY VARIANT 7731"
SECRET_MEDIA = "https://media.example.test/maxfiy-7731.png"
FORBIDDEN_KEYS = {"text", "options", "media_url", "media_provider", "explanation", "primary_answer",
                  "accepted_answers", "correct_answer", "correct_answers", "answer"}


def _secret_question(db_session, n):
    q = Question(text=f"{SECRET_TEXT} #{n}", points=2, status="approved", explanation=f"{SECRET_EXPLANATION} #{n}",
                 options=[f"{SECRET_OPTION} A{n}", f"{SECRET_OPTION} B{n}"], media_url=SECRET_MEDIA,
                 media_provider="url")
    db_session.add(q)
    db_session.flush()
    db_session.add(AcceptedAnswer(question_id=q.id, answer_text=f"{SECRET_ANSWER} #{n}", is_primary=True))
    return q


def _assert_no_question_content(res):
    assert res.status_code == status.HTTP_200_OK
    body = res.text
    for secret in (SECRET_TEXT, SECRET_ANSWER, SECRET_EXPLANATION, SECRET_OPTION, SECRET_MEDIA):
        assert secret not in body, f"public detail leaks: {secret}"
    data = res.json()
    for rnd in data["rounds"]:
        for q in rnd["questions"]:
            assert set(q.keys()) == {"sequence", "points"}
            assert not (set(q.keys()) & FORBIDDEN_KEYS)
    return data


# ---------------------------------------------------------------------------
# SEC-01
# ---------------------------------------------------------------------------

def test_public_detail_manifest_path_exposes_no_question_content(client, db_session):
    quiz = Quiz(title="Secret Manifest Pack", description="d")
    db_session.add(quiz)
    db_session.flush()
    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="modern_multiround")
    db_session.add(version)
    db_session.flush()
    rnd = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db_session.add(rnd)
    db_session.flush()
    qs = []
    for n in (1, 2, 3):
        q = _secret_question(db_session, n)
        rq = RoundQuestion(round_id=rnd.id, question_id=q.id, sequence=n)
        db_session.add(rq)
        db_session.flush()
        qs.append({"question_id": q.id, "round_question_id": rq.id, "sequence": n, "text": q.text, "points": 2,
                   "options": q.options, "media_url": q.media_url, "media_provider": "url",
                   "explanation": q.explanation, "primary_answer": f"{SECRET_ANSWER} #{n}",
                   "accepted_answers": [{"answer_text": f"{SECRET_ANSWER} #{n}", "is_primary": True}]})
    version.published_manifest = {"title": quiz.title, "game_mode": "modern_multiround",
                                  "rounds": [{"round_id": rnd.id, "sequence": 1, "round_type": "standard", "questions": qs}]}
    db_session.commit()

    data = _assert_no_question_content(client.get(f"/api/quizzes/{quiz.id}"))
    # The detail view still gets what it needs: structure, counts and points
    assert data["total_questions"] == 3
    assert len(data["rounds"][0]["questions"]) == 3
    assert data["rounds"][0]["questions"][0] == {"sequence": 1, "points": 2}


def test_public_detail_database_fallback_path_exposes_no_question_content(client, db_session):
    quiz = Quiz(title="Secret Legacy Pack", description="d")
    db_session.add(quiz)
    db_session.flush()
    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="modern_multiround")
    db_session.add(version)
    db_session.flush()
    rnd = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db_session.add(rnd)
    db_session.flush()
    for n in (1, 2):
        q = _secret_question(db_session, n)
        db_session.add(RoundQuestion(round_id=rnd.id, question_id=q.id, sequence=n))
    db_session.commit()
    assert version.published_manifest is None  # forces the database fallback path

    data = _assert_no_question_content(client.get(f"/api/quizzes/{quiz.id}"))
    assert data["total_questions"] == 2


def test_public_arena_listing_exposes_no_question_content(client, db_session):
    test_public_detail_manifest_path_exposes_no_question_content(client, db_session)
    listing = client.get("/api/quizzes")
    assert listing.status_code == status.HTTP_200_OK
    for secret in (SECRET_TEXT, SECRET_ANSWER, SECRET_EXPLANATION, SECRET_OPTION, SECRET_MEDIA):
        assert secret not in listing.text


def test_detail_modal_only_uses_question_counts():
    """Frontend dependency check: the quiz detail modal only needs the per-round question COUNT
    (plus the point values, which the public stub already carries) -- never question text."""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    fn = html[html.index("async function openQuizDetail("):html.index("function closeQuizDetailModal()")]
    assert "const questions = r.questions || [];" in fn
    assert "${questions.length} ta savol" in fn
    used = set(re.findall(r"\bq\.(\w+)", fn[fn.index("const questions = r.questions"):fn.index("pack-detail-round-meta")]))
    assert used == {"points"}


# ---------------------------------------------------------------------------
# SEC-04
# ---------------------------------------------------------------------------

@pytest.fixture
def pack(db_session):
    quiz = Quiz(title="Resume Pack", description="2 savol")
    db_session.add(quiz)
    db_session.flush()
    _add_version(db_session, quiz, 1)
    return quiz


def test_reopening_pack_during_round_reveal_resumes_same_ranked_attempt(client, db_session, pack):
    user = _make_user(db_session, "reveal_resume@zakowhat.uz", "Reveal Resumer")
    h = _headers(user)
    token = client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=h).json()["session_token"]
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"}, headers=h)
    client.post(f"/api/play/{token}/answer", json={"answer": "A2"}, headers=h)
    assert _attempt(db_session, token).status == "round_reveal"

    # Player goes back to the Arena and opens the same pack again
    res = client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=h)
    assert res.status_code == status.HTTP_201_CREATED
    data = res.json()
    assert data["session_token"] == token
    assert data["resumed"] is True and data["restore_required"] is True
    assert data["status"] == "round_reveal"
    assert data["ranked"] is True
    assert data["current_state"] is None

    att = _attempt(db_session, token)
    assert att.status == "round_reveal"  # NOT abandoned
    assert db_session.query(SoloAttempt).filter(SoloAttempt.user_id == user.id).count() == 1

    # The restored reveal works and the ranked result is recorded
    assert client.get(f"/api/play/{token}", headers=h).json()["status"] == "round_reveal"
    assert client.get(f"/api/play/{token}/reveal", headers=h).status_code == status.HTTP_200_OK
    assert client.post(f"/api/play/{token}/continue", headers=h).json()["status"] == "completed"
    board = _board(client, pack)
    assert len(board) == 1 and board[0]["correct_count"] == 2


def test_reopening_pack_during_intermediate_break_resumes_same_attempt(client, db_session):
    user = _make_user(db_session, "break_resume@zakowhat.uz")
    h = _headers(user)
    quiz = create_classic_zakovat(db_session, title="Break Resume")
    token = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "with_timer"}, headers=h).json()["session_token"]
    for n in range(1, 13):
        ans = client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {n}"}, headers=h).json()
        if ans.get("awaiting_continue"):
            client.post(f"/api/play/{token}/continue", headers=h)
    assert _attempt(db_session, token).status == "intermediate_break"

    data = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "with_timer"}, headers=h).json()
    assert data["session_token"] == token and data["status"] == "intermediate_break" and data["restore_required"] is True
    assert _attempt(db_session, token).status == "intermediate_break"


def test_reopening_pack_during_reveal_pause_never_leaks_next_question(client, db_session):
    user = _make_user(db_session, "pause_resume@zakowhat.uz")
    h = _headers(user)
    quiz = create_classic_zakovat(db_session, title="Pause Resume")
    token = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "with_timer"}, headers=h).json()["session_token"]
    assert client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"}, headers=h).json()["awaiting_continue"] is True

    res = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "with_timer"}, headers=h)
    data = res.json()
    assert data["session_token"] == token
    assert data["awaiting_continue"] is True and data["restore_required"] is True
    assert data["current_state"] is None
    assert "Reveal savol 2" not in res.text  # the withheld next question is not exposed
    att = _attempt(db_session, token)
    assert att.status == "in_progress" and att.question_opened_at is None  # clock still stopped


def test_frontend_restores_paused_stages_from_server_state():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "if (data.restore_required) {" in html
    assert "await restoreActiveSession();" in html


# ---------------------------------------------------------------------------
# SEC-09
# ---------------------------------------------------------------------------

def _answer_late(client, db_session, token, headers, answer, seconds_late=400):
    att = _attempt(db_session, token)
    att.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_late)
    db_session.commit()
    return client.post(f"/api/play/{token}/answer", json={"answer": answer}, headers=headers).json()


def test_ranked_run_keeps_strict_timeout_penalty(client, db_session, pack):
    user = _make_user(db_session, "strict_ranked@zakowhat.uz")
    h = _headers(user)
    token = client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=h).json()["session_token"]
    assert _attempt(db_session, token).attempt_metadata["competitive"] is True
    res = _answer_late(client, db_session, token, h, "A1")
    assert res["timed_out"] is True
    assert res["is_correct"] is False and res["points_awarded"] == 0


def test_timed_practice_run_gets_forgiving_timeout(client, db_session, pack):
    user = _make_user(db_session, "forgiving_practice@zakowhat.uz")
    h = _headers(user)
    client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=h)
    token = client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer", "restart": True},
                        headers=h).json()["session_token"]
    att = _attempt(db_session, token)
    assert att.timer_mode == "standard" and att.attempt_metadata["competitive"] is False  # timed practice
    res = _answer_late(client, db_session, token, h, "A1")
    assert res["timed_out"] is False
    assert res["is_correct"] is True and res["points_awarded"] == 1
    db_session.refresh(att)
    assert att.active_time_seconds < 400  # time is still capped at the question limit


def test_legacy_timed_attempt_keeps_strict_timeout(client, db_session, pack):
    """Historical timed attempts (no competitive flag) keep the strict behaviour they always had."""
    user = _make_user(db_session, "strict_legacy@zakowhat.uz")
    h = _headers(user)
    token = client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=h).json()["session_token"]
    att = _attempt(db_session, token)
    meta = dict(att.attempt_metadata)
    meta.pop("competitive")
    att.attempt_metadata = meta
    db_session.commit()
    res = _answer_late(client, db_session, token, h, "A1")
    assert res["timed_out"] is True and res["points_awarded"] == 0


def test_timeout_logic_uses_authoritative_flag():
    with open("api/play.py", "r", encoding="utf-8") as f:
        src = f.read()
    assert "practice_mode" not in src
    assert "is_competitive = is_ranked_or_legacy_timed(attempt)" in src


# ---------------------------------------------------------------------------
# REV-01: /review never exposes answers of rounds still to be played
# ---------------------------------------------------------------------------

def test_review_rejected_during_mid_game_round_reveal(client, db_session, seed_sample_quiz):
    """Multi-round pack: after round 1 the attempt is in round_reveal; /review must not return the
    answers of rounds 2-4 (it did, exposing them to a ranked player before they were played)."""
    user = _make_user(db_session, "review_midgame@zakowhat.uz")
    h = _headers(user)
    token = client.post(f"/api/play/start/{seed_sample_quiz.id}", json={"timer_mode": "with_timer"},
                        headers=h).json()["session_token"]
    for _ in range(20):
        ans = client.post(f"/api/play/{token}/answer", json={"answer": "xato"}, headers=h).json()
        if ans.get("round_completed"):
            break
    att = _attempt(db_session, token)
    assert att.status == "round_reveal" and att.current_round_index == 0  # mid-game reveal

    review = client.get(f"/api/play/{token}/review", headers=h)
    assert review.status_code == status.HTTP_400_BAD_REQUEST
    assert "accepted_answers" not in review.text and "correct_answers" not in review.text
    # The round reveal itself still works (current round only)
    assert client.get(f"/api/play/{token}/reveal", headers=h).status_code == status.HTTP_200_OK


def test_review_allowed_at_final_round_reveal_and_after_completion(client, db_session, pack):
    user = _make_user(db_session, "review_final@zakowhat.uz")
    h = _headers(user)
    token = client.post(f"/api/play/start/{pack.id}", json={"timer_mode": "with_timer"}, headers=h).json()["session_token"]
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"}, headers=h)
    client.post(f"/api/play/{token}/answer", json={"answer": "A2"}, headers=h)
    assert _attempt(db_session, token).status == "round_reveal"  # final (only) round
    assert client.get(f"/api/play/{token}/review", headers=h).status_code == status.HTTP_200_OK
    client.post(f"/api/play/{token}/continue", headers=h)
    assert _attempt(db_session, token).status == "completed"
    assert client.get(f"/api/play/{token}/review", headers=h).status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# DEP-01: CSRF origin check behind a TLS-terminating proxy
# ---------------------------------------------------------------------------

def test_csrf_accepts_same_host_https_origin_behind_tls_proxy(client):
    """Railway terminates TLS: the app sees http while the browser sends Origin: https://<host>.
    Same-host origins must pass; foreign hosts (incl. look-alikes) must still be rejected."""
    from fastapi.testclient import TestClient
    from main import app

    proxied = TestClient(app, base_url="http://zakowhat.example")
    cases = {
        "https://zakowhat.example": status.HTTP_200_OK,          # real browser behind TLS proxy
        "http://zakowhat.example": status.HTTP_200_OK,
        "https://evil.example": status.HTTP_403_FORBIDDEN,
        "https://zakowhat.example.evil.example": status.HTTP_403_FORBIDDEN,
        "https://evil-zakowhat.example": status.HTTP_403_FORBIDDEN,
    }
    for origin, expected in cases.items():
        res = proxied.post("/api/auth/logout", headers={"Origin": origin})
        assert res.status_code == expected, origin
