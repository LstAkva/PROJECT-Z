"""Pre-launch security, authorization, privacy boundary, and account lifecycle tests."""
import secrets
import pytest
from fastapi.testclient import TestClient
from models import User, SoloAttempt, QuizVersion, Quiz, DailyQuestion, DailySubmission
from datetime import date, datetime, timezone


def test_unauthorized_profile_mutation_rejected(client: TestClient):
    """Anonymous visitors must be rejected with 401 when mutating profile endpoints."""
    # Ensure client is unauthenticated
    client.cookies.clear()

    # PATCH /api/auth/profile
    res = client.patch("/api/auth/profile", json={"display_name": "Hacker"})
    assert res.status_code == 401

    # POST /api/auth/avatar
    res = client.post("/api/auth/avatar", json={"image_data": "invalid"})
    assert res.status_code == 401

    # DELETE /api/auth/avatar
    res = client.delete("/api/auth/avatar")
    assert res.status_code == 401

    # POST /api/auth/change-password
    res = client.post("/api/auth/change-password", json={"current_password": "x", "new_password": "y"})
    assert res.status_code == 401

    # DELETE /api/auth/account
    res = client.request("DELETE", "/api/auth/account", json={"confirm_text": "DELETE"})
    assert res.status_code == 401


def test_private_profile_data_protection(client: TestClient, db_session):
    """When a profile is private, bio, stats, and sensitive data must be suppressed."""
    u_suffix = secrets.token_hex(4)
    email = f"priv_{u_suffix}@example.com"
    pwd = "Password123!"

    reg = client.post("/api/auth/register", json={
        "email": email,
        "password": pwd,
        "display_name": f"Secret Agent {u_suffix}",
    })
    assert reg.status_code in (200, 201)

    # Set username, bio, and is_public=False
    patch_res = client.patch("/api/auth/profile", json={
        "username": f"agent_{u_suffix}",
        "bio": "Top secret intelligence operative.",
        "is_public": False,
        "show_stats": False,
    })
    assert patch_res.status_code == 200

    # Query public endpoint by username
    pub_res = client.get(f"/api/users/agent_{u_suffix}/public")
    assert pub_res.status_code == 200
    pub_data = pub_res.json()

    assert pub_data["is_private"] is True
    assert pub_data.get("bio") is None
    assert pub_data.get("stats") is None
    assert "email" not in pub_data
    assert "hashed_password" not in pub_data
    assert "password_hash" not in pub_data


def test_avatar_upload_security_validation(client: TestClient):
    """Avatar upload must reject oversized files, non-images, and malformed base64."""
    u_suffix = secrets.token_hex(4)
    email = f"av_{u_suffix}@example.com"
    client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "display_name": f"Avatar User {u_suffix}",
    })

    # 1. Malformed base64
    res = client.post("/api/auth/avatar", json={"image_data": "not-valid-base64-content!!!"})
    assert res.status_code == 400

    # 2. Text file disguised as base64 (no magic bytes)
    import base64
    txt_b64 = base64.b64encode(b"hello world this is just a plain text file").decode("utf-8")
    res = client.post("/api/auth/avatar", json={"image_data": txt_b64})
    assert res.status_code == 400
    assert "PNG, JPG yoki WebP" in res.json()["detail"]

    # 3. Oversized file (>2MB)
    huge_data = b"\x89PNG\r\n\x1a\n" + (b"0" * (2 * 1024 * 1024 + 100))
    huge_b64 = base64.b64encode(huge_data).decode("utf-8")
    res = client.post("/api/auth/avatar", json={"image_data": huge_b64})
    assert res.status_code == 400
    assert "2 MB" in res.json()["detail"]


def test_account_deletion_cleans_daily_submissions_and_anonymizes_attempts(client: TestClient, db_session):
    """Account deletion must clean user rows, avatar, daily submissions, and anonymize attempts."""
    u_suffix = secrets.token_hex(4)
    email = f"del_{u_suffix}@example.com"
    pwd = "Password123!"

    reg = client.post("/api/auth/register", json={
        "email": email,
        "password": pwd,
        "display_name": f"Delete User {u_suffix}",
    })
    assert reg.status_code in (200, 201)
    user_id = reg.json()["user"]["id"]

    # Create dummy quiz and attempt
    quiz = Quiz(title=f"Delete Test Quiz {u_suffix}")
    db_session.add(quiz)
    db_session.flush()

    qv = QuizVersion(quiz_id=quiz.id, version_number=1, status="published")
    db_session.add(qv)
    db_session.flush()

    attempt = SoloAttempt(
        quiz_version_id=qv.id,
        user_id=user_id,
        session_token=f"sess_{u_suffix}",
        status="in_progress",
    )
    db_session.add(attempt)

    # Create dummy daily question and submission
    dq = DailyQuestion(
        schedule_date=date(2026, 12, 1),
        question_id=f"dq_{u_suffix}",
        message_id=99999,
        question_text="Sample daily question?",
        primary_answer="Ans",
        accepted_answers=["Ans"],
    )
    db_session.add(dq)
    db_session.flush()

    dsub = DailySubmission(
        daily_question_id=dq.id,
        user_id=user_id,
        submitted_text="Ans",
        is_correct=True,
    )
    db_session.add(dsub)
    db_session.commit()

    # Wrong confirmation text rejected
    res_wrong = client.request("DELETE", "/api/auth/account", json={"confirm_text": "WRONG_CONFIRM"})
    assert res_wrong.status_code == 400

    # Valid confirmation text: 'DELETE'
    res_del = client.request("DELETE", "/api/auth/account", json={"confirm_text": "DELETE"})
    assert res_del.status_code == 200

    # User record must be deleted
    user_in_db = db_session.query(User).filter(User.id == user_id).first()
    assert user_in_db is None

    # Attempt must be anonymized (user_id is None)
    att_in_db = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == f"sess_{u_suffix}").first()
    assert att_in_db is not None
    assert att_in_db.user_id is None

    # Daily submission must be cleaned
    dsub_in_db = db_session.query(DailySubmission).filter(DailySubmission.daily_question_id == dq.id).first()
    assert dsub_in_db is None


def test_consent_endpoint_validation_and_persistence(client: TestClient):
    """POST /api/auth/consent must accept valid choices ('necessary', 'all') and reject garbage."""
    # Anonymous request
    res = client.post("/api/auth/consent", json={"cookie_consent": "all"})
    assert res.status_code == 200
    assert res.json()["cookie_consent"] == "all"

    res_nec = client.post("/api/auth/consent", json={"cookie_consent": "necessary"})
    assert res_nec.status_code == 200
    assert res_nec.json()["cookie_consent"] == "necessary"
