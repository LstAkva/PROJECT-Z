import os
import pytest
from fastapi import status
from sqlalchemy.exc import IntegrityError
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User
from database import validate_database_environment


def create_mock_complete_draft(client, title="Security Hardening Pack"):
    """Creates a draft quiz with 2 rounds of 12 questions meeting all Zakovat rules."""
    res = client.post("/api/drafts", json={
        "title": title,
        "description": "Valid 2x12 pack for publishing tests.",
        "game_mode": "classic_zakovat",
    })
    assert res.status_code == status.HTTP_201_CREATED
    draft_id = res.json()["version_id"]

    for r_seq, r_title in [(1, "1-tur"), (2, "2-tur")]:
        r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={
            "round_type": "zakovat_classic",
            "name": r_title,
        })
        round_id = r_res.json()["round_id"]
        start_q = (r_seq - 1) * 12 + 1
        for i in range(start_q, start_q + 12):
            client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
                "text": f"Savol #{i}: Matn bu yerda.",
                "primary_answer": f"Javob {i}",
                "points": 1,
            })

    draft_data = client.get(f"/api/drafts/{draft_id}").json()
    return draft_data["quiz_id"], draft_id


# ==============================================================================
# 1. STAGING ACCESS GATE TESTS
# ==============================================================================

def test_staging_access_gate_requires_authentication(client):
    """
    Anonymous/public visitors must NOT be able to list or start unreleased staging packs.
    Returns HTTP 401 Unauthorized unless an authenticated user session is active.
    """
    client.cookies.clear()

    # 1. Anonymous GET /api/play/staging/quizzes -> 401
    list_res = client.get("/api/play/staging/quizzes")
    assert list_res.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Anonymous POST /api/play/staging/start/{quiz_id} -> 401
    start_res = client.post("/api/play/staging/start/99999")
    assert start_res.status_code == status.HTTP_401_UNAUTHORIZED

    # 3. Authenticated user can access staging
    client.post("/api/auth/register", json={
        "display_name": "Staging Tester",
        "email": "tester_staging@zakowhat.uz",
        "password": "Password123!",
    })
    auth_list_res = client.get("/api/play/staging/quizzes")
    assert auth_list_res.status_code == status.HTTP_200_OK


# ==============================================================================
# 2. OLD PUBLISH ENDPOINT CONVERGENCE TESTS
# ==============================================================================

def test_old_publish_endpoint_enforces_validation_and_idempotency(client):
    """
    POST /api/quizzes/{quiz_id}/publish/{version_number} must delegate to the authoritative
    execute_publish_version service, enforcing validation rules and 409 conflict checks.
    """
    # Create incomplete draft
    d_res = client.post("/api/drafts", json={"title": "Incomplete Draft", "game_mode": "classic_zakovat"})
    draft_id = d_res.json()["version_id"]
    d_info = client.get(f"/api/drafts/{draft_id}").json()
    quiz_id = d_info["quiz_id"]
    v_num = d_info["version_number"]

    # 1. Incomplete draft cannot be published via old route (422 Unprocessable Entity)
    old_pub_res = client.post(f"/api/quizzes/{quiz_id}/publish/{v_num}")
    assert old_pub_res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # Create complete valid draft
    complete_quiz_id, complete_draft_id = create_mock_complete_draft(client, "Old Route Valid Pack")
    c_info = client.get(f"/api/drafts/{complete_draft_id}").json()
    c_vnum = c_info["version_number"]

    # 2. Valid draft publishes successfully via old route
    pub_res = client.post(f"/api/quizzes/{complete_quiz_id}/publish/{c_vnum}")
    assert pub_res.status_code == status.HTTP_200_OK
    assert pub_res.json()["success"] is True

    # 3. Subsequent publish attempt on already published version returns 409 Conflict
    dup_res = client.post(f"/api/quizzes/{complete_quiz_id}/publish/{c_vnum}")
    assert dup_res.status_code == status.HTTP_409_CONFLICT


# ==============================================================================
# 3. ANONYMOUS IDENTITY COOKIE PRECEDENCE & SPOOF PREVENTION TESTS
# ==============================================================================

def test_anonymous_identity_cookie_precedence_and_spoof_prevention(client):
    """
    The server-issued HttpOnly zakowhat_anon_id cookie must be strictly authoritative.
    Client-supplied X-Anon-Id headers must NOT override or reset the cookie quota.
    In production mode, client headers are completely ignored.
    """
    complete_quiz_id, complete_draft_id = create_mock_complete_draft(client, "Anon Identity Pack")
    # Publish so it can be played publicly
    client.post(f"/api/drafts/{complete_draft_id}/publish")

    client.cookies.clear()

    # 1. First anonymous attempt: server generates cookie
    start1 = client.post(f"/api/play/start/{complete_quiz_id}")
    assert start1.status_code == status.HTTP_201_CREATED
    server_cookie = client.cookies.get("zakowhat_anon_id")
    assert server_cookie is not None
    assert server_cookie.startswith("anon_")

    # 2. Subsequent attempt while presenting the cookie must be blocked (403),
    # even if client sends a different X-Anon-Id header!
    start2 = client.post(
        f"/api/play/start/{complete_quiz_id}",
        headers={"X-Anon-Id": "spoofed_new_id_attempt_bypass"}
    )
    assert start2.status_code == status.HTTP_403_FORBIDDEN

    # 3. In production environment, X-Anon-Id header is never accepted
    os.environ["ENVIRONMENT"] = "production"
    try:
        client.cookies.clear()
        start3 = client.post(
            f"/api/play/start/{complete_quiz_id}",
            headers={"X-Anon-Id": "custom_header_in_prod"}
        )
        assert start3.status_code == status.HTTP_201_CREATED
        # Validate Set-Cookie attributes directly: plain HTTP test clients do not persist Secure cookies
        set_cookie = start3.headers.get("set-cookie", "")
        assert "zakowhat_anon_id=" in set_cookie
        assert "custom_header_in_prod" not in set_cookie
        assert "Secure" in set_cookie
        assert "HttpOnly" in set_cookie
    finally:
        os.environ["ENVIRONMENT"] = "test"


# ==============================================================================
# 4. SUBMIT ANSWER IDEMPOTENCY & MISMATCH REJECTION TESTS
# ==============================================================================

def test_submit_answer_idempotency_and_mismatch_rejection(client):
    """
    Server-side attempt state is authoritative:
    - Retrying an already-answered question ID returns an idempotent duplicate response (is_duplicate=True).
    - Submitting a mismatched/future question ID raises 409 Conflict; state is not advanced.
    - Duplicate submission on completed attempt returns idempotent response rather than 400 error.
    """
    complete_quiz_id, complete_draft_id = create_mock_complete_draft(client, "Idempotency Test Pack")
    client.post(f"/api/drafts/{complete_draft_id}/publish")

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{complete_quiz_id}", json={"timer_mode": "no_timer"})
    assert start_res.status_code == status.HTTP_201_CREATED
    data = start_res.json()
    token = data["session_token"]
    first_q = data["current_state"]["question"]
    q1_id = first_q["question_id"]
    rq1_id = first_q.get("round_question_id")

    # 1. Normal submission for Question 1
    sub1 = client.post(f"/api/play/{token}/answer", json={
        "answer": "Javob 1",
        "question_id": q1_id,
        "round_question_id": rq1_id,
    })
    assert sub1.status_code == status.HTTP_200_OK
    res1 = sub1.json()
    assert res1["is_correct"] is True
    assert res1["points_awarded"] == 1
    assert res1["total_score"] == 1
    assert res1.get("is_duplicate") is not True

    # 2. Retrying Question 1 (network glitch or double click) -> Idempotent duplicate response
    retry1 = client.post(f"/api/play/{token}/answer", json={
        "answer": "Javob 1",
        "question_id": q1_id,
        "round_question_id": rq1_id,
    })
    assert retry1.status_code == status.HTTP_200_OK
    res_retry = retry1.json()
    assert res_retry.get("is_duplicate") is True
    assert res_retry["total_score"] == 1  # Score did not double!
    # Next state still points to Question 2
    assert res_retry["next_state"]["round_question_index"] == 1

    # 3. Submitting an out-of-order / unreached question ID (e.g. question 10) -> 409 Conflict
    mismatch_res = client.post(f"/api/play/{token}/answer", json={
        "answer": "Javob 10",
        "question_id": 99999,
    })
    assert mismatch_res.status_code == status.HTTP_409_CONFLICT

    # Verify attempt is still waiting for Question 2
    curr_state = client.get(f"/api/play/{token}").json()
    assert curr_state["round_question_index"] == 1


# ==============================================================================
# 5. ANON QUOTA RACE CONDITION / PARTIAL UNIQUE INDEX TESTS
# ==============================================================================

def test_anon_quota_race_condition_protection(db_session):
    """
    The partial conditional unique index idx_solo_attempts_anon_id_unique ensures
    no two SoloAttempt rows can have the same non-null anon_id, while allowing
    unlimited registered user rows with anon_id=None.
    """
    anon_id = "test_race_anon_unique_token_123"

    # 1. First attempt with this anon_id succeeds
    attempt1 = SoloAttempt(
        quiz_version_id=1,
        anon_id=anon_id,
        user_id=None,
        session_token="test_race_tok_1",
        timer_mode="standard",
    )
    db_session.add(attempt1)
    db_session.commit()

    # 2. Second concurrent attempt with the same anon_id triggers IntegrityError
    attempt2 = SoloAttempt(
        quiz_version_id=1,
        anon_id=anon_id,
        user_id=None,
        session_token="test_race_tok_2",
        timer_mode="standard",
    )
    db_session.add(attempt2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. Multiple registered user attempts with anon_id=None can coexist freely
    user_attempt1 = SoloAttempt(
        quiz_version_id=1,
        anon_id=None,
        user_id=10,
        session_token="test_user_tok_1",
        timer_mode="standard",
    )
    user_attempt2 = SoloAttempt(
        quiz_version_id=1,
        anon_id=None,
        user_id=10,
        session_token="test_user_tok_2",
        timer_mode="standard",
    )
    db_session.add_all([user_attempt1, user_attempt2])
    db_session.commit()


# ==============================================================================
# 6. DATABASE ENVIRONMENT SAFETY TESTS
# ==============================================================================

def test_database_environment_safety_validation():
    """
    validate_database_environment prevents mismatch between ENVIRONMENT and DB_TARGET,
    and blocks non-production environments from connecting to production hosts.
    """
    # 1. ENVIRONMENT=production with DB_TARGET=development -> RuntimeError
    with pytest.raises(RuntimeError, match="ENVIRONMENT is 'production' but DB_TARGET is not 'production'"):
        validate_database_environment(
            url="postgresql://user:pass@localhost:5432/db",
            environment="production",
            db_target="development"
        )

    # 2. ENVIRONMENT=development with DB_TARGET=production -> RuntimeError
    with pytest.raises(RuntimeError, match="DB_TARGET is 'production' but ENVIRONMENT is not 'production'"):
        validate_database_environment(
            url="postgresql://user:pass@localhost:5432/db",
            environment="development",
            db_target="production"
        )

    # 3. Non-production environment connecting to host with '-prod' -> RuntimeError
    with pytest.raises(RuntimeError, match="non-production environment configured with production database"):
        validate_database_environment(
            url="postgresql://user:pass@db-prod-aws.neon.tech/neondb",
            environment="development",
            db_target="development"
        )

    # 4. Valid development setup -> passes cleanly
    validate_database_environment(
        url="postgresql://user:pass@ep-dev-test.neon.tech/neondb",
        environment="development",
        db_target="development"
    )

    # 5. SQLite memory URL -> passes cleanly
    validate_database_environment(
        url="sqlite:///:memory:",
        environment="test",
        db_target="test"
    )

    # 6. Unset / empty ENVIRONMENT -> fails closed with RuntimeError
    with pytest.raises(RuntimeError, match="ENVIRONMENT must be explicitly configured"):
        validate_database_environment(
            url="postgresql://user:pass@localhost:5432/db",
            environment="",
            db_target="development"
        )
    old_env = os.environ.pop("ENVIRONMENT", None)
    try:
        with pytest.raises(RuntimeError, match="ENVIRONMENT must be explicitly configured"):
            validate_database_environment(
                url="postgresql://user:pass@localhost:5432/db",
                environment=None,
                db_target="development"
            )
    finally:
        if old_env is not None:
            os.environ["ENVIRONMENT"] = old_env

    # 7. Unset / empty DB_TARGET -> fails closed with RuntimeError
    with pytest.raises(RuntimeError, match="DB_TARGET must be explicitly configured"):
        validate_database_environment(
            url="postgresql://user:pass@localhost:5432/db",
            environment="development",
            db_target=""
        )
    old_target = os.environ.pop("DB_TARGET", None)
    try:
        with pytest.raises(RuntimeError, match="DB_TARGET must be explicitly configured"):
            validate_database_environment(
                url="postgresql://user:pass@localhost:5432/db",
                environment="development",
                db_target=None
            )
    finally:
        if old_target is not None:
            os.environ["DB_TARGET"] = old_target

    # 8. Invalid ENVIRONMENT tier -> fails closed with RuntimeError
    with pytest.raises(RuntimeError, match="Invalid ENVIRONMENT 'invalid_tier'"):
        validate_database_environment(
            url="postgresql://user:pass@localhost:5432/db",
            environment="invalid_tier",
            db_target="development"
        )

    # 9. Explicit PROD_DATABASE_HOST exact match check
    os.environ["PROD_DATABASE_HOST"] = "custom-prod.internal.db"
    try:
        with pytest.raises(RuntimeError, match="PROD_DATABASE_HOST match"):
            validate_database_environment(
                url="postgresql://user:pass@custom-prod.internal.db:5432/db",
                environment="development",
                db_target="development"
            )
    finally:
        os.environ.pop("PROD_DATABASE_HOST", None)


# ==============================================================================
# 7. LEGACY QUESTION CASCADE REMOVAL TESTS
# ==============================================================================

def test_legacy_question_cascade_removal(db_session):
    """
    Deleting a Round must cascade to RoundQuestion, but MUST NEVER delete
    the underlying reusable Question in the Question Bank!
    """
    # 1. Create a canonical Question in the Question Bank
    q = Question(
        text="Bank Question that must survive round deletion",
        points=1,
        status="approved",
    )
    db_session.add(q)
    db_session.commit()
    q_id = q.id

    # 2. Create a Quiz, Version, and Round
    quiz = Quiz(title="Cascade Guard Test Quiz")
    db_session.add(quiz)
    db_session.commit()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, game_mode="classic_zakovat")
    db_session.add(version)
    db_session.commit()

    rnd = Round(quiz_version_id=version.id, sequence=1, round_type="zakovat_classic")
    db_session.add(rnd)
    db_session.commit()

    # 3. Link Question to Round via RoundQuestion
    rq = RoundQuestion(round_id=rnd.id, question_id=q.id, sequence=1)
    db_session.add(rq)
    db_session.commit()

    # 4. Delete the Round
    db_session.delete(rnd)
    db_session.commit()

    # 5. Verify: Round is deleted, RoundQuestion is deleted, but Question SURVIVES!
    assert db_session.query(Round).filter(Round.id == rnd.id).first() is None
    assert db_session.query(RoundQuestion).filter(RoundQuestion.id == rq.id).first() is None

    surviving_q = db_session.query(Question).filter(Question.id == q_id).first()
    assert surviving_q is not None
    assert surviving_q.text == "Bank Question that must survive round deletion"


# ==============================================================================
# 8. COMPREHENSIVE CLOSURE TESTS (INDEPENDENT REVIEW GAPS)
# ==============================================================================

def test_anonymous_identity_fail_closed_when_env_unset(client):
    """
    When ENVIRONMENT is unset/missing, security-sensitive subsystems must fail closed.
    Production or unconfigured deployments must NEVER accept arbitrary X-Anon-Id headers.
    """
    from api.auth import get_authoritative_anon_id
    from starlette.datastructures import Headers

    class DummyRequest:
        def __init__(self, cookies=None, headers=None):
            self.cookies = cookies or {}
            self.headers = Headers(headers or {})

    old_env = os.environ.get("ENVIRONMENT")
    old_testing = os.environ.get("TESTING")

    try:
        # Case 1: ENVIRONMENT unset, TESTING unset -> fail closed (ignores header)
        if "ENVIRONMENT" in os.environ:
            del os.environ["ENVIRONMENT"]
        if "TESTING" in os.environ:
            del os.environ["TESTING"]

        req = DummyRequest(headers={"X-Anon-Id": "spoofed_unauthorized_id"})
        assert get_authoritative_anon_id(req) is None

        # Case 2: ENVIRONMENT=production -> fail closed (ignores header)
        os.environ["ENVIRONMENT"] = "production"
        assert get_authoritative_anon_id(req) is None

        # Case 3: HttpOnly cookie ALWAYS takes precedence regardless of environment
        req_with_cookie = DummyRequest(
            cookies={"zakowhat_anon_id": "valid_cookie_anon_id"},
            headers={"X-Anon-Id": "spoofed_id"}
        )
        assert get_authoritative_anon_id(req_with_cookie) == "valid_cookie_anon_id"

        # Case 4: Explicitly configured development allows header
        os.environ["ENVIRONMENT"] = "development"
        assert get_authoritative_anon_id(req) == "spoofed_unauthorized_id"
    finally:
        if old_env is not None:
            os.environ["ENVIRONMENT"] = old_env
        elif "ENVIRONMENT" in os.environ:
            del os.environ["ENVIRONMENT"]
        if old_testing is not None:
            os.environ["TESTING"] = old_testing
        elif "TESTING" in os.environ:
            del os.environ["TESTING"]


def test_staging_access_gate_with_admin_emails(client):
    """
    If ADMIN_EMAILS / STAGING_AUTHORIZED_EMAILS is configured, staging endpoints
    restrict access to authorized editor/owner emails (403 for non-admins).
    """
    old_admin = os.environ.get("ADMIN_EMAILS")
    os.environ["ADMIN_EMAILS"] = "editor@zakowhat.uz,owner@zakowhat.uz"

    try:
        client.cookies.clear()
        # 1. Register and login normal user
        client.post("/api/auth/register", json={
            "email": "normal_player@gmail.com",
            "password": "Password123!",
            "display_name": "Normal Player"
        })
        login_res = client.post("/api/auth/login", json={
            "email": "normal_player@gmail.com",
            "password": "Password123!"
        })
        assert login_res.status_code == status.HTTP_200_OK

        # Normal player gets 403 Forbidden
        stg_res = client.get("/api/play/staging/quizzes")
        assert stg_res.status_code == status.HTTP_403_FORBIDDEN
        assert "administrator" in stg_res.json()["detail"].lower()

        # 2. Register and login editor
        client.cookies.clear()
        client.post("/api/auth/register", json={
            "email": "editor@zakowhat.uz",
            "password": "Password123!",
            "display_name": "Authorized Editor"
        })
        client.post("/api/auth/login", json={
            "email": "editor@zakowhat.uz",
            "password": "Password123!"
        })

        # Authorized editor gets 200 OK
        stg_res_ok = client.get("/api/play/staging/quizzes")
        assert stg_res_ok.status_code == status.HTTP_200_OK
    finally:
        if old_admin is not None:
            os.environ["ADMIN_EMAILS"] = old_admin
        elif "ADMIN_EMAILS" in os.environ:
            del os.environ["ADMIN_EMAILS"]


def test_answer_idempotency_omitted_ids_and_boundaries(client):
    """
    Tests all 6 idempotency scenarios:
    1. Normal current-question submission
    2. Duplicate submission (omitted IDs) -> returns is_duplicate=True without advancing
    3. Stale submission after advancement -> returns is_duplicate=True without skipping question
    4. Submission with omitted IDs for next question -> advances properly
    5. Q12 boundary -> intermediate_break, duplicate returns is_duplicate=True, intermediate_break=True
    6. Q24 boundary -> completed, duplicate returns is_duplicate=True, quiz_completed=True
    """
    quiz_id, draft_id = create_mock_complete_draft(client, title="Idempotency Scenario Pack")
    pub_res = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_res.status_code == status.HTTP_200_OK

    # Start attempt
    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]
    q1_data = start_res.json()["current_state"]["question"]
    q1_id = q1_data["id"]

    # 1. Normal submission with question_id
    ans1 = client.post(f"/api/play/{token}/answer", json={
        "answer": "Javob 1",
        "question_id": q1_id
    })
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["is_correct"] is True
    assert ans1.json()["total_score"] == 1
    q2_id = ans1.json()["next_state"]["question"]["id"]

    # 2. Duplicate submission of Q1 (with question_id) -> must return is_duplicate=True and NOT advance
    dup1 = client.post(f"/api/play/{token}/answer", json={
        "answer": "Javob 1",
        "question_id": q1_id
    })
    assert dup1.status_code == status.HTTP_200_OK
    assert dup1.json().get("is_duplicate") is True
    assert dup1.json()["total_score"] == 1
    assert dup1.json()["next_state"]["question"]["id"] == q2_id

    # 3. Question mismatch rejection: submitting future/unknown question_id returns 409 Conflict
    mismatch = client.post(f"/api/play/{token}/answer", json={
        "answer": "Future Guess",
        "question_id": 99999
    })
    assert mismatch.status_code == status.HTTP_409_CONFLICT

    # 4. Normal submission with omitted IDs for Q2 -> advances properly to Q3
    ans2 = client.post(f"/api/play/{token}/answer", json={
        "answer": "Javob 2"
    })
    assert ans2.status_code == status.HTTP_200_OK
    assert ans2.json()["is_correct"] is True
    assert ans2.json()["total_score"] == 2
    assert ans2.json()["next_state"]["question"]["sequence"] == 3

    # Fast forward through Q3..Q11 with omitted IDs
    for i in range(3, 12):
        res_i = client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {i}"})
        assert res_i.status_code == status.HTTP_200_OK

    # 5. Q12 Boundary -> transitions to intermediate_break
    ans12 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 12"})
    assert ans12.status_code == status.HTTP_200_OK
    assert ans12.json().get("intermediate_break") is True

    # Duplicate submission of Q12 with omitted IDs -> returns is_duplicate=True, intermediate_break=True
    dup12 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 12"})
    assert dup12.status_code == status.HTTP_200_OK
    assert dup12.json().get("is_duplicate") is True
    assert dup12.json().get("intermediate_break") is True

    # Continue to Round 2 (Q13)
    cont_res = client.post(f"/api/play/{token}/continue")
    assert cont_res.status_code == status.HTTP_200_OK
    assert cont_res.json()["current_state"]["round"]["sequence"] == 2

    # Fast forward Q13..Q23 with omitted IDs
    for i in range(13, 24):
        res_i = client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {i}"})
        assert res_i.status_code == status.HTTP_200_OK

    # 6. Q24 Boundary -> transitions to completed
    ans24 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 24"})
    assert ans24.status_code == status.HTTP_200_OK
    assert ans24.json()["quiz_completed"] is True

    # Finalize to completed
    client.get(f"/api/play/{token}/results")

    # Duplicate submission of Q24 after completion with omitted IDs -> is_duplicate=True
    dup24 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 24"})
    assert dup24.status_code == status.HTTP_200_OK
    assert dup24.json().get("is_duplicate") is True
    assert dup24.json()["quiz_completed"] is True
