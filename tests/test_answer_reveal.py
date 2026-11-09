"""
Per-question answer reveal ("Davom etish") — Task 1.

- Classic Zakovat / Classic Svoyak: after each answer the server returns the correct answer, the player's
  answer, and the explanation ONLY when a genuine one exists. The next question is withheld and its clock
  stays stopped until the player presses "Davom etish" (POST /continue).
- Explanations are optional data: missing / empty / literal "None" explanations are never shown or invented.
"""
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import status

from api.auth import create_access_token, hash_password
from api.play import clean_explanation
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User
from tests.test_svoyak_gameplay import create_svoyak_test_game


@pytest.fixture
def reveal_user(db_session):
    user = User(email="reveal_player@zakowhat.uz", display_name="Reveal Player",
                hashed_password=hash_password("Pass123!"), auth_provider="local")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


def create_classic_zakovat(db_session, explanations=None, title="Reveal Zakovat"):
    """2 rounds x 12 classic Zakovat questions. `explanations` maps global question number -> raw explanation."""
    explanations = explanations or {}
    quiz = Quiz(title=title, description="2x12")
    db_session.add(quiz)
    db_session.flush()
    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="classic_zakovat")
    db_session.add(version)
    db_session.flush()
    manifest = []
    n = 0
    for r_seq in (1, 2):
        rnd = Round(quiz_version_id=version.id, sequence=r_seq, round_type="zakovat_classic", config={"name": f"{r_seq}-tur"})
        db_session.add(rnd)
        db_session.flush()
        qs = []
        for q_seq in range(1, 13):
            n += 1
            expl = explanations.get(n)
            q = Question(text=f"Reveal savol {n}", points=1, status="approved", explanation=expl)
            db_session.add(q)
            db_session.flush()
            a = AcceptedAnswer(question_id=q.id, answer_text=f"Javob {n}", is_primary=True)
            db_session.add(a)
            rq = RoundQuestion(round_id=rnd.id, question_id=q.id, sequence=q_seq)
            db_session.add(rq)
            db_session.flush()
            qs.append({"question_id": q.id, "round_question_id": rq.id, "sequence": q_seq, "text": q.text,
                       "points": 1, "primary_answer": a.answer_text, "explanation": expl,
                       "accepted_answers": [{"answer_text": a.answer_text, "is_primary": True}]})
        manifest.append({"round_id": rnd.id, "sequence": r_seq, "round_type": "zakovat_classic",
                         "config": {"name": f"{r_seq}-tur"}, "questions": qs})
    version.published_manifest = {"title": title, "game_mode": "classic_zakovat", "rounds": manifest}
    db_session.commit()
    return quiz


def _start(client, quiz, headers, **body):
    res = client.post(f"/api/play/start/{quiz.id}", json=body, headers=headers)
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()["session_token"]


# ---------------------------------------------------------------------------
# Explanation is optional
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "None", "none", "null", "NaN", "-"])
def test_clean_explanation_treats_missing_markers_as_absent(raw):
    assert clean_explanation(raw) is None


def test_clean_explanation_keeps_genuine_text():
    assert clean_explanation("  Haqiqiy izoh matni.  ") == "Haqiqiy izoh matni."


def test_reveal_always_has_answers_and_only_genuine_explanation(client, db_session, reveal_user):
    quiz = create_classic_zakovat(db_session, explanations={1: "Bu haqiqiy izoh.", 2: "None", 3: ""})
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")

    # Q1: genuine explanation, wrong answer
    r1 = client.post(f"/api/play/{token}/answer", json={"answer": "Noto'g'ri"}, headers=headers).json()
    assert r1["correct_answer"] == "Javob 1"
    assert r1["submitted_answer"] == "Noto'g'ri"
    assert r1["explanation"] == "Bu haqiqiy izoh."
    client.post(f"/api/play/{token}/continue", headers=headers)

    # Q2: literal "None" in data -> no explanation shown, answers still present
    r2 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 2"}, headers=headers).json()
    assert r2["correct_answer"] == "Javob 2"
    assert r2["submitted_answer"] == "Javob 2"
    assert r2["explanation"] is None
    client.post(f"/api/play/{token}/continue", headers=headers)

    # Q3: empty explanation + blank answer -> submitted_answer None, explanation None
    r3 = client.post(f"/api/play/{token}/answer", json={"answer": ""}, headers=headers).json()
    assert r3["correct_answer"] == "Javob 3"
    assert r3["submitted_answer"] is None
    assert r3["explanation"] is None


# ---------------------------------------------------------------------------
# Pacing: next question withheld, clock stopped until "Davom etish"
# ---------------------------------------------------------------------------

def test_next_question_clock_starts_only_on_continue(client, db_session, reveal_user):
    quiz = create_classic_zakovat(db_session, title="Reveal Clock")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")

    res = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"}, headers=headers).json()
    assert res["awaiting_continue"] is True
    assert res["next_state"] is None

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    db_session.refresh(attempt)
    assert attempt.question_opened_at is None  # no clock running during the reveal
    assert attempt.attempt_metadata.get("awaiting_continue") is True

    cont = client.post(f"/api/play/{token}/continue", headers=headers)
    assert cont.status_code == status.HTTP_200_OK
    assert cont.json()["current_state"]["round_question_index"] == 1
    db_session.refresh(attempt)
    assert attempt.question_opened_at is not None
    assert not attempt.attempt_metadata.get("awaiting_continue")


def test_reading_the_reveal_never_causes_a_timeout(client, db_session, reveal_user):
    """Reveal time is not charged to the next question: answering right after /continue is on time."""
    quiz = create_classic_zakovat(db_session, title="Reveal No Timeout")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")
    client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"}, headers=headers)

    # Simulate a long reveal: whatever happened before /continue is irrelevant because the clock was stopped
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    meta = dict(attempt.attempt_metadata)
    meta["last_active_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    attempt.attempt_metadata = meta
    db_session.commit()

    client.post(f"/api/play/{token}/continue", headers=headers)
    r2 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 2"}, headers=headers).json()
    assert r2["timed_out"] is False
    assert r2["is_correct"] is True
    assert r2["total_score"] == 2


def test_refresh_during_reveal_restores_reveal_not_next_question(client, db_session, reveal_user):
    quiz = create_classic_zakovat(db_session, explanations={1: "Izoh bir."}, title="Reveal Refresh")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")
    client.post(f"/api/play/{token}/answer", json={"answer": "xato"}, headers=headers)

    state = client.get(f"/api/play/{token}", headers=headers).json()
    assert state["status"] == "in_progress"
    assert state["awaiting_continue"] is True
    assert state["current_state"] is None
    assert "Reveal savol 2" not in str(state)  # next question text never leaks
    last = state["last_reveal"]
    assert last["question_text"] == "Reveal savol 1"
    assert last["correct_answer"] == "Javob 1"
    assert last["submitted_answer"] == "xato"
    assert last["explanation"] == "Izoh bir."
    assert last["is_correct"] is False


def test_answer_during_reveal_pause_is_rejected(client, db_session, reveal_user):
    """Direct API attack: answering the withheld next question without /continue (untimed, zero recorded time)
    is rejected with 409; nothing is recorded and play continues normally after "Davom etish"."""
    from models import AnswerRecord

    quiz = create_classic_zakovat(db_session, title="Reveal Blind")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")
    client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"}, headers=headers)
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    db_session.refresh(attempt)
    time_before = attempt.active_time_seconds

    for body in ({"answer": "Javob 2"}, {"answer": ""}, {"answer": "Javob 2", "client_submission_id": "fresh-key-1"}):
        blind = client.post(f"/api/play/{token}/answer", json=body, headers=headers)
        assert blind.status_code == status.HTTP_409_CONFLICT
        assert "Davom etish" in blind.json()["detail"]

    db_session.refresh(attempt)
    assert db_session.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).count() == 1
    assert attempt.active_time_seconds == time_before
    assert attempt.attempt_metadata.get("awaiting_continue") is True

    assert client.post(f"/api/play/{token}/continue", headers=headers).status_code == status.HTTP_200_OK
    ok = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 2"}, headers=headers)
    assert ok.status_code == status.HTTP_200_OK and ok.json()["is_correct"] is True


def test_q12_break_and_q24_completion_carry_the_reveal(client, db_session, reveal_user):
    quiz = create_classic_zakovat(db_session, explanations={12: "O'n ikkinchi izoh."}, title="Reveal Boundaries")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")

    for n in range(1, 12):
        client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {n}"}, headers=headers)
        client.post(f"/api/play/{token}/continue", headers=headers)

    q12 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 12"}, headers=headers).json()
    assert q12["intermediate_break"] is True
    assert q12["correct_answer"] == "Javob 12"
    assert q12["explanation"] == "O'n ikkinchi izoh."

    cont = client.post(f"/api/play/{token}/continue", headers=headers).json()
    assert cont["current_state"]["round"]["sequence"] == 2

    for n in range(13, 24):
        client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {n}"}, headers=headers)
        client.post(f"/api/play/{token}/continue", headers=headers)

    q24 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 24"}, headers=headers).json()
    assert q24["round_completed"] is True
    assert q24["quiz_completed"] is True
    assert q24["correct_answer"] == "Javob 24"
    assert q24["explanation"] is None
    assert q24["total_score"] == 24


# ---------------------------------------------------------------------------
# Scope: Svoyak and other modes
# ---------------------------------------------------------------------------

def test_free_choice_svoyak_reveals_without_pacing(client, db_session, reveal_user):
    quiz = create_svoyak_test_game(client, db_session, "Reveal Free Choice")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, svoyak_mode="free_choice")
    client.post(f"/api/play/{token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    res = client.post(f"/api/play/{token}/answer", json={"answer": "xato"}, headers=headers).json()
    assert res["correct_answer"] == "Javob 1_10"
    assert res["submitted_answer"] == "xato"
    assert res["explanation"] == "Mavzu 1 10 izohi"
    assert res["status"] == "board_selection"
    assert not res.get("awaiting_continue")


def test_non_paced_modes_are_unchanged(client, seed_sample_quiz, reveal_user):
    """Modern multi-round (standard/zanjir/true-false) keeps the old contract: no reveal fields, next_state returned."""
    headers = _headers(reveal_user)
    token = _start(client, seed_sample_quiz, headers)
    res = client.post(f"/api/play/{token}/answer", json={"answer": "anything"}, headers=headers).json()
    assert "correct_answer" not in res
    assert "explanation" not in res
    assert not res.get("awaiting_continue")
    # Old routing preserved: either the next question is returned directly, or the round ends as before
    assert res["next_state"] is not None or res["round_completed"] is True


def test_restart_clears_pending_reveal(client, db_session, reveal_user):
    quiz = create_classic_zakovat(db_session, title="Reveal Restart")
    headers = _headers(reveal_user)
    token = _start(client, quiz, headers, timer_mode="with_timer")
    client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"}, headers=headers)

    restarted = client.post(f"/api/play/{token}/restart", headers=headers).json()
    assert restarted["current_state"]["round_question_index"] == 0
    # Ranked attempt is abandoned by the restart; the new session starts clean (no pending reveal)
    new_token = restarted["session_token"]
    assert new_token != token
    state = client.get(f"/api/play/{new_token}", headers=headers).json()
    assert not state.get("awaiting_continue")
    assert state["round_question_index"] == 0


# ---------------------------------------------------------------------------
# Client template
# ---------------------------------------------------------------------------

def test_reveal_panel_template():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert 'id="answer-reveal-panel" class="hidden' in html
    assert 'id="reveal-submitted-answer"' in html
    assert 'id="reveal-correct-answer"' in html
    # Explanation section starts hidden and is shown only for a non-empty explanation
    assert 'id="reveal-explanation-wrap" class="hidden' in html
    assert "explWrap.classList.toggle('hidden', !explanation)" in html
    assert 'onclick="handleRevealContinue()"' in html
    assert "Davom etish" in html
    # Reveal is driven by the server payload; pacing goes through /continue
    assert "Object.prototype.hasOwnProperty.call(result, 'correct_answer')" in html
    assert "async function continueToNextQuestion()" in html
    assert "/continue`, { method: 'POST' }" in html
    # Refresh during a reveal restores the reveal
    assert "data.status === 'in_progress' && data.awaiting_continue" in html
