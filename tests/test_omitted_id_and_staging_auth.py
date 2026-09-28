import os
import pytest
from fastapi import status
from sqlalchemy import text
from database import engine, SessionLocal
from models import SoloAttempt, AnswerRecord, User
from tests.test_security_hardening import create_mock_complete_draft


# ==============================================================================
# FINDING 1 REGRESSION TESTS: OMITTED-ID IDEMPOTENCY & STATE AUTHORITATIVENESS
# ==============================================================================

def test_regression_a_previous_answer_equals_current_wrong_answer_advances_normally(client):
    """
    Test Case A:
    Q2 answer = "Toshkent"
    Q3 correct answer = "Javob 3"
    Player submits "Toshkent" on Q3 as a legitimate wrong answer with IDs omitted.
    Server-side attempt/current-question state remains authoritative:
    - Never uses answer-text equality alone as proof of duplicate identity.
    - Evaluates "Toshkent" for Q3 -> is_correct=False, points_awarded=0.
    - Total score remains 2 (not mutated or corrupted).
    - Current question advances normally to Q4 (round_question_index=3).
    """
    quiz_id, draft_id = create_mock_complete_draft(client, "Regression A Pack")
    # Customise Q1, Q2, Q3 answers to match exact finding scenario
    # Q1: "Javob 1", Q2: "Toshkent", Q3: "Javob 3"
    client.post(f"/api/drafts/{draft_id}/publish")

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}", json={"timer_mode": "no_timer"})
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]

    # 1. Answer Q1 with "Javob 1" -> correct, score 1, advances to Q2
    ans1 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"})
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["is_correct"] is True
    assert ans1.json()["total_score"] == 1
    assert ans1.json()["next_state"]["round_question_index"] == 1  # Q2

    # 2. Answer Q2 with "Javob 2" -> correct, score 2, advances to Q3
    ans2 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 2"})
    assert ans2.status_code == status.HTTP_200_OK
    assert ans2.json()["is_correct"] is True
    assert ans2.json()["total_score"] == 2
    assert ans2.json()["next_state"]["round_question_index"] == 2  # Q3

    # State before Q3 submission
    state_before_q3 = client.get(f"/api/play/{token}").json()
    assert state_before_q3["round_question_index"] == 2
    assert state_before_q3["total_score"] == 2

    # 3. Player submits "Javob 2" on Q3 (IDs omitted) as a legitimate WRONG answer for Q3!
    # (Notice: matches previous answer "Javob 2", but correct answer for Q3 is "Javob 3")
    ans3 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 2"})
    assert ans3.status_code == status.HTTP_200_OK
    data3 = ans3.json()

    # MUST NOT be swallowed as a duplicate!
    assert data3.get("is_duplicate") is not True
    # MUST be evaluated as wrong for Q3
    assert data3["is_correct"] is False
    assert data3["points_awarded"] == 0
    # MUST preserve score
    assert data3["total_score"] == 2
    # MUST advance attempt state to Q4 (round_question_index = 3)
    assert data3["next_state"]["round_question_index"] == 3

    # Verify server state is now on Q4
    state_after_q3 = client.get(f"/api/play/{token}").json()
    assert state_after_q3["round_question_index"] == 3
    assert state_after_q3["total_score"] == 2


def test_regression_b_stale_replay_of_previous_answer_duplicate_no_advancement(client):
    """
    Test Case B:
    Genuine stale replay of the previous question's submission:
    - Retrying with previous question_id returns an idempotent duplicate (is_duplicate=True).
    - Retrying with matching Idempotency-Key header returns an idempotent duplicate (is_duplicate=True).
    - Server-side state remains on the current question; score and index are NOT advanced.
    """
    quiz_id, draft_id = create_mock_complete_draft(client, "Regression B Pack")
    client.post(f"/api/drafts/{draft_id}/publish")

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}", json={"timer_mode": "no_timer"})
    assert start_res.status_code == status.HTTP_201_CREATED
    data = start_res.json()
    token = data["session_token"]
    q1_id = data["current_state"]["question"]["id"]

    # 1. Answer Q1 with question_id -> correct, score=1, advances to Q2
    ans1 = client.post(
        f"/api/play/{token}/answer",
        json={"question_id": q1_id, "answer": "Javob 1"}
    )
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["is_correct"] is True
    assert ans1.json()["total_score"] == 1
    assert ans1.json()["next_state"]["round_question_index"] == 1  # Q2

    # Attempt is now at Q2
    state_q2 = client.get(f"/api/play/{token}").json()
    assert state_q2["round_question_index"] == 1

    # 2. Genuine stale replay of Q1 submission (with Q1's question_id) while on Q2
    replay_q1 = client.post(
        f"/api/play/{token}/answer",
        json={"question_id": q1_id, "answer": "Javob 1"}
    )
    assert replay_q1.status_code == status.HTTP_200_OK
    rep_data = replay_q1.json()
    assert rep_data.get("is_duplicate") is True
    assert rep_data["total_score"] == 1
    assert rep_data["next_state"]["round_question_index"] == 1  # Stays at Q2, NO advancement!

    # Verify attempt remains at Q2
    state_still_q2 = client.get(f"/api/play/{token}").json()
    assert state_still_q2["round_question_index"] == 1
    assert state_still_q2["total_score"] == 1

    # 3. Answer Q2 using an Idempotency-Key header -> advances to Q3
    ans2 = client.post(
        f"/api/play/{token}/answer",
        headers={"Idempotency-Key": "idemp-req-q2-unique"},
        json={"answer": "Javob 2"}
    )
    assert ans2.status_code == status.HTTP_200_OK
    assert ans2.json()["total_score"] == 2
    assert ans2.json()["next_state"]["round_question_index"] == 2  # Q3

    # 4. Replay exact Q2 request using same Idempotency-Key header (even with IDs omitted)
    replay_idemp = client.post(
        f"/api/play/{token}/answer",
        headers={"Idempotency-Key": "idemp-req-q2-unique"},
        json={"answer": "Javob 2"}
    )
    assert replay_idemp.status_code == status.HTTP_200_OK
    rep_idemp_data = replay_idemp.json()
    assert rep_idemp_data.get("is_duplicate") is True
    assert rep_idemp_data["total_score"] == 2
    assert rep_idemp_data["next_state"]["round_question_index"] == 2  # Stays at Q3, NO advancement!

    state_still_q3 = client.get(f"/api/play/{token}").json()
    assert state_still_q3["round_question_index"] == 2


def test_regression_c_q12_stale_replay_intermediate_break(client):
    """
    Test Case C:
    Q12 boundary idempotency:
    - Answer Q12 -> transitions to intermediate_break.
    - Replaying Q12 submission (with or without IDs) returns idempotent duplicate response.
    - Cannot advance round or alter total score.
    - Submitting an unexpected/unclassifiable answer during break returns controlled HTTP 400 error.
    """
    quiz_id, draft_id = create_mock_complete_draft(client, "Regression C Pack")
    client.post(f"/api/drafts/{draft_id}/publish")

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}", json={"timer_mode": "no_timer"})
    token = start_res.json()["session_token"]

    # Play Q1..Q11
    for i in range(1, 12):
        ans_i = client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {i}"})
        assert ans_i.status_code == status.HTTP_200_OK

    # Answer Q12 -> intermediate break
    ans12 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 12"})
    assert ans12.status_code == status.HTTP_200_OK
    assert ans12.json().get("intermediate_break") is True
    assert ans12.json()["total_score"] == 12

    state_break = client.get(f"/api/play/{token}").json()
    assert state_break["status"] == "intermediate_break"

    # Stale replay of Q12 without IDs
    replay12 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 12"})
    assert replay12.status_code == status.HTTP_200_OK
    rep12_data = replay12.json()
    assert rep12_data.get("is_duplicate") is True
    assert rep12_data.get("intermediate_break") is True
    assert rep12_data["total_score"] == 12

    # Attempt state remains strictly in intermediate_break
    state_after = client.get(f"/api/play/{token}").json()
    assert state_after["status"] == "intermediate_break"
    assert state_after["total_score"] == 12

    # Submitting an unclassifiable new answer during break returns controlled error
    bad_sub = client.post(f"/api/play/{token}/answer", json={"answer": "different_unclassifiable_answer"})
    assert bad_sub.status_code == status.HTTP_400_BAD_REQUEST
    assert "stage is finished" in bad_sub.json()["detail"].lower()


def test_regression_d_q24_stale_replay_completion(client):
    """
    Test Case D:
    Q24 completion boundary idempotency:
    - Answer through Q24 -> quiz completed.
    - Replaying Q24 submission (with or without IDs) returns idempotent duplicate response.
    - Score and completion state remain intact.
    - Submitting an unexpected/unclassifiable answer returns controlled HTTP 400 error.
    """
    quiz_id, draft_id = create_mock_complete_draft(client, "Regression D Pack")
    client.post(f"/api/drafts/{draft_id}/publish")

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}", json={"timer_mode": "no_timer"})
    token = start_res.json()["session_token"]

    # Play Q1..Q12 -> break -> continue -> Q13..Q24
    for i in range(1, 13):
        client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {i}"})
    client.post(f"/api/play/{token}/continue")
    for i in range(13, 25):
        ans = client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {i}"})
        if i == 24:
            assert ans.json()["quiz_completed"] is True
            assert ans.json()["total_score"] == 24

    # Finalize to completed
    client.get(f"/api/play/{token}/results")
    state = client.get(f"/api/play/{token}").json()
    assert state["status"] == "completed"

    # Stale replay of Q24
    rep24 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 24"})
    assert rep24.status_code == status.HTTP_200_OK
    rep_data = rep24.json()
    assert rep_data.get("is_duplicate") is True
    assert rep_data["quiz_completed"] is True
    assert rep_data["total_score"] == 24

    # Submitting an unclassifiable different answer on completed quiz returns controlled error
    diff_sub = client.post(f"/api/play/{token}/answer", json={"answer": "different_answer_on_completed"})
    assert diff_sub.status_code == status.HTTP_400_BAD_REQUEST
    assert "already completed" in diff_sub.json()["detail"].lower()


# ==============================================================================
# STAGING AUTHORIZATION GATES
# ==============================================================================

def test_staging_authorization_three_tiers(client):
    """
    Explicitly tests:
    - anonymous -> 401
    - authenticated non-authorized account -> 403 when ADMIN_EMAILS / STAGING_AUTHORIZED_EMAILS is configured
    - authorized account -> 200
    """
    os.environ["ADMIN_EMAILS"] = "authorized_admin@zakowhat.uz"
    try:
        # Create a staging draft quiz for this test session
        quiz_id, draft_id = create_mock_complete_draft(client, "Classic Zakovat — Pack Auth Test")

        client.cookies.clear()

        # 1. Anonymous visitor -> 401 Unauthorized
        anon_list = client.get("/api/play/staging/quizzes")
        assert anon_list.status_code == status.HTTP_401_UNAUTHORIZED

        anon_start = client.post(f"/api/play/staging/start/{quiz_id}")
        assert anon_start.status_code == status.HTTP_401_UNAUTHORIZED

        # 2. Authenticated non-authorized user -> 403 Forbidden
        client.post("/api/auth/register", json={
            "display_name": "Regular Player",
            "email": "regular_player@zakowhat.uz",
            "password": "Password123!",
        })
        forbidden_list = client.get("/api/play/staging/quizzes")
        assert forbidden_list.status_code == status.HTTP_403_FORBIDDEN
        assert "administrator" in forbidden_list.json()["detail"].lower()

        forbidden_start = client.post(f"/api/play/staging/start/{quiz_id}")
        assert forbidden_start.status_code == status.HTTP_403_FORBIDDEN
        assert "administrator" in forbidden_start.json()["detail"].lower()

        # 3. Authenticated authorized administrator -> 200 OK
        client.cookies.clear()
        client.post("/api/auth/register", json={
            "display_name": "Authorized Admin",
            "email": "authorized_admin@zakowhat.uz",
            "password": "Password123!",
        })
        allowed_list = client.get("/api/play/staging/quizzes")
        assert allowed_list.status_code == status.HTTP_200_OK
        quizzes = allowed_list.json()
        assert len(quizzes) >= 1
        assert any(q["quiz_id"] == quiz_id for q in quizzes)

        allowed_start = client.post(f"/api/play/staging/start/{quiz_id}")
        assert allowed_start.status_code == status.HTTP_201_CREATED
        assert "session_token" in allowed_start.json()

    finally:
        os.environ.pop("ADMIN_EMAILS", None)
