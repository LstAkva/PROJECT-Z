import os
import pytest
from datetime import datetime, timezone
from fastapi import status
from sqlalchemy.orm import Session

from models import Quiz, QuizVersion, SoloAttempt, AnswerRecord, User
from api.auth import decode_access_token


def test_registration_validation_and_duplicate_handling(client):
    """
    Milestone Requirement 1:
    - Registration validates email format, password length, and display name.
    - Duplicate email registration is safely rejected with HTTP 409 Conflict.
    - Successful registration returns 201 and sets HttpOnly auth cookie.
    """
    client.cookies.clear()

    # 1. Invalid email format -> 422
    res_bad_email = client.post("/api/auth/register", json={
        "display_name": "Alisher",
        "email": "not-an-email",
        "password": "Password123!",
    })
    assert res_bad_email.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "email" in res_bad_email.json()["detail"].lower()

    # 2. Empty display name -> 422
    res_empty_name = client.post("/api/auth/register", json={
        "display_name": "   ",
        "email": "alisher@zakowhat.uz",
        "password": "Password123!",
    })
    assert res_empty_name.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 3. Short password (< 6 chars) -> 422
    res_short_pwd = client.post("/api/auth/register", json={
        "display_name": "Alisher",
        "email": "alisher@zakowhat.uz",
        "password": "123",
    })
    assert res_short_pwd.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 4. Valid registration -> 201 Created + HttpOnly access_token cookie
    reg_res = client.post("/api/auth/register", json={
        "display_name": "Alisher Navoiy",
        "email": "alisher@zakowhat.uz",
        "password": "ValidPassword123!",
    })
    assert reg_res.status_code == status.HTTP_201_CREATED
    data = reg_res.json()
    assert data["success"] is True
    assert data["user"]["display_name"] == "Alisher Navoiy"
    assert data["user"]["email"] == "alisher@zakowhat.uz"
    assert "access_token" in client.cookies

    # 5. Duplicate email registration -> 409 Conflict
    client.cookies.clear()
    dup_res = client.post("/api/auth/register", json={
        "display_name": "Boshqa Ism",
        "email": "alisher@zakowhat.uz",
        "password": "AnotherPassword123!",
    })
    assert dup_res.status_code == status.HTTP_409_CONFLICT
    assert "allaqachon mavjud" in dup_res.json()["detail"].lower()


def test_login_credentials_and_session_handling(client):
    """
    Milestone Requirement 1 & 5:
    - Login handles correct/incorrect credentials with clear feedback.
    - Password verified via bcrypt.
    - Clean logout clears the HttpOnly access_token cookie.
    """
    client.cookies.clear()

    # Register user
    reg = client.post("/api/auth/register", json={
        "display_name": "Zahiriddin",
        "email": "bobur@zakowhat.uz",
        "password": "StrongPassword789!",
    })
    assert reg.status_code == status.HTTP_201_CREATED

    client.cookies.clear()

    # 1. Wrong password -> 401 Unauthorized
    res_wrong_pw = client.post("/api/auth/login", json={
        "email": "bobur@zakowhat.uz",
        "password": "WrongPassword!",
    })
    assert res_wrong_pw.status_code == status.HTTP_401_UNAUTHORIZED
    assert "noto'g'ri" in res_wrong_pw.json()["detail"].lower()
    assert "access_token" not in client.cookies

    # 2. Non-existent user -> 401 Unauthorized
    res_no_user = client.post("/api/auth/login", json={
        "email": "unknown@zakowhat.uz",
        "password": "StrongPassword789!",
    })
    assert res_no_user.status_code == status.HTTP_401_UNAUTHORIZED

    # 3. Successful login by email -> 200 + auth cookie
    res_ok = client.post("/api/auth/login", json={
        "email": "bobur@zakowhat.uz",
        "password": "StrongPassword789!",
    })
    assert res_ok.status_code == status.HTTP_200_OK
    assert "access_token" in client.cookies
    assert res_ok.json()["user"]["display_name"] == "Zahiriddin"

    # 4. Successful login by username / display_name
    client.cookies.clear()
    res_user_ok = client.post("/api/auth/login", json={
        "email": "Zahiriddin",
        "password": "StrongPassword789!",
    })
    assert res_user_ok.status_code == status.HTTP_200_OK
    assert "access_token" in client.cookies

    # 5. /api/auth/me returns authenticated status
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == status.HTTP_200_OK
    assert me_res.json()["authenticated"] is True
    assert me_res.json()["user"]["email"] == "bobur@zakowhat.uz"

    # 6. Logout clears cookie
    logout_res = client.post("/api/auth/logout")
    assert logout_res.status_code == status.HTTP_200_OK
    assert "access_token" not in client.cookies or client.cookies.get("access_token") == ""

    me_after = client.get("/api/auth/me")
    assert me_after.status_code == status.HTTP_200_OK
    assert me_after.json()["authenticated"] is False


def test_protected_gameplay_requires_authentication(client, seed_sample_quiz, db_session):
    """
    Milestone Requirement 2:
    - Unauthenticated start requests are strictly rejected with 401 Unauthorized.
    - Anonymous gameplay is not allowed.
    - Authenticated player starting an attempt has attempt.user_id set.
    """
    quiz = seed_sample_quiz

    # 1. Unauthenticated start attempt -> 401 Unauthorized
    client.cookies.clear()
    unauth_start = client.post(f"/api/play/start/{quiz.id}")
    assert unauth_start.status_code == status.HTTP_401_UNAUTHORIZED
    assert "tizimga kiring" in unauth_start.json()["detail"].lower() or "ro'yxatdan o'ting" in unauth_start.json()["detail"].lower()

    # 2. Register & authenticate player
    client.post("/api/auth/register", json={
        "display_name": "Temur",
        "email": "temur@zakowhat.uz",
        "password": "Password123!",
    })

    # 3. Authenticated start -> 201 Created
    auth_start = client.post(f"/api/play/start/{quiz.id}")
    assert auth_start.status_code == status.HTTP_201_CREATED
    data = auth_start.json()
    token = data["session_token"]
    assert token is not None

    # Verify attempt in DB is bound to authenticated user
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    assert attempt is not None
    assert attempt.user_id is not None
    user = db_session.query(User).filter(User.email == "temur@zakowhat.uz").first()
    assert attempt.user_id == user.id
    assert attempt.anon_id is None


def test_attempt_isolation_and_ownership_enforcement(client, seed_sample_quiz, db_session):
    """
    Milestone Requirement 4:
    - Protect against users accessing or modifying other users' private gameplay sessions.
    - User B cannot submit answers, reveal, continue, or view results/review for User A's session (403 Forbidden).
    """
    quiz = seed_sample_quiz

    # 1. User A registers and starts a game
    client.cookies.clear()
    client.post("/api/auth/register", json={
        "display_name": "User A",
        "email": "user_a@zakowhat.uz",
        "password": "Password123!",
    })
    start_a = client.post(f"/api/play/start/{quiz.id}")
    token_a = start_a.json()["session_token"]

    # 2. User B registers and acquires their own session cookie
    client.cookies.clear()
    client.post("/api/auth/register", json={
        "display_name": "User B",
        "email": "user_b@zakowhat.uz",
        "password": "Password123!",
    })

    # User B tries to get current state of User A's attempt -> 403 Forbidden
    res_state = client.get(f"/api/play/{token_a}")
    assert res_state.status_code == status.HTTP_403_FORBIDDEN
    assert "boshqa foydalanuvchining" in res_state.json()["detail"].lower()

    # User B tries to submit answer to User A's attempt -> 403 Forbidden
    res_ans = client.post(f"/api/play/{token_a}/answer", json={"answer": "Toshkent"})
    assert res_ans.status_code == status.HTTP_403_FORBIDDEN

    # User B tries to access reveal on User A's attempt -> 403 Forbidden
    res_rev = client.get(f"/api/play/{token_a}/reveal")
    assert res_rev.status_code == status.HTTP_403_FORBIDDEN

    # User B tries to continue User A's attempt -> 403 Forbidden
    res_cont = client.post(f"/api/play/{token_a}/continue")
    assert res_cont.status_code == status.HTTP_403_FORBIDDEN

    # User B tries to get results of User A's attempt -> 403 Forbidden
    res_res = client.get(f"/api/play/{token_a}/results")
    assert res_res.status_code == status.HTTP_403_FORBIDDEN

    # User B tries to get review of User A's attempt -> 403 Forbidden
    res_revw = client.get(f"/api/play/{token_a}/review")
    assert res_revw.status_code == status.HTTP_403_FORBIDDEN

    # 3. User A logs back in and can play normally
    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "user_a@zakowhat.uz", "password": "Password123!"})
    res_owner_state = client.get(f"/api/play/{token_a}")
    assert res_owner_state.status_code == status.HTTP_200_OK

    res_owner_ans = client.post(f"/api/play/{token_a}/answer", json={"answer": "Toshkent"})
    assert res_owner_ans.status_code == status.HTTP_200_OK
    assert res_owner_ans.json()["is_correct"] is True


def test_player_profile_and_persistent_progress(client, seed_sample_quiz, db_session):
    """
    Milestone Requirement 3 & 4:
    - GET /api/auth/profile returns account info and aggregated statistics.
    - Statistics aggregate only completed non-preview attempts.
    - User B's profile is completely isolated from User A's history.
    """
    quiz = seed_sample_quiz

    # 1. Unauthenticated profile request -> 401
    client.cookies.clear()
    unauth_prof = client.get("/api/auth/profile")
    assert unauth_prof.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Register Player 1
    reg1 = client.post("/api/auth/register", json={
        "display_name": "Mirzo Ulug'bek",
        "email": "ulugbek@zakowhat.uz",
        "password": "Password123!",
    })
    user1_id = reg1.json()["user"]["id"]

    # Initial profile: 0 games, 0 score
    prof0 = client.get("/api/auth/profile").json()
    assert prof0["user"]["display_name"] == "Mirzo Ulug'bek"
    assert prof0["stats"]["total_games"] == 0
    assert prof0["stats"]["total_score"] == 0
    assert prof0["stats"]["total_correct"] == 0
    assert len(prof0["recent_games"]) == 0

    # 3. Play and complete 1 game (4 rounds in seed_sample_quiz)
    start_res = client.post(f"/api/play/start/{quiz.id}")
    token = start_res.json()["session_token"]

    # R1: Toshkent -> +1
    client.post(f"/api/play/{token}/answer", json={"answer": "Toshkent"})
    client.post(f"/api/play/{token}/continue")
    # R2: rost -> +1
    client.post(f"/api/play/{token}/answer", json={"answer": "rost"})
    client.post(f"/api/play/{token}/continue")
    # R3: olma (+2), limon (+2)
    client.post(f"/api/play/{token}/answer", json={"answer": "olma"})
    client.post(f"/api/play/{token}/answer", json={"answer": "limon"})
    client.post(f"/api/play/{token}/continue")
    # R4: quyosh (+1), shahar (+1), rishton (+1)
    client.post(f"/api/play/{token}/answer", json={"answer": "quyosh"})
    client.post(f"/api/play/{token}/answer", json={"answer": "shahar"})
    client.post(f"/api/play/{token}/answer", json={"answer": "rishton"})
    cont_final = client.post(f"/api/play/{token}/continue")
    assert cont_final.json()["quiz_completed"] is True

    # 4. Check profile: stats must reflect the completed game!
    prof1 = client.get("/api/auth/profile").json()
    assert prof1["stats"]["total_games"] == 1
    assert prof1["stats"]["total_correct"] == 7
    assert prof1["stats"]["total_score"] == 9  # 1 + 1 + 2 + 2 + 1 + 1 + 1
    assert prof1["stats"]["accuracy_percent"] == 100.0
    assert len(prof1["recent_games"]) == 1
    game_rec = prof1["recent_games"][0]
    assert game_rec["quiz_title"] == "ZakoWhat Test Championship"
    assert game_rec["total_score"] == 9
    assert game_rec["total_correct"] == 7
    assert game_rec["status"] == "completed"

    # 5. User 2 registers: must see ZERO games and no access to User 1's history
    client.cookies.clear()
    client.post("/api/auth/register", json={
        "display_name": "Ibn Sino",
        "email": "ibnsino@zakowhat.uz",
        "password": "Password123!",
    })
    prof2 = client.get("/api/auth/profile").json()
    assert prof2["user"]["display_name"] == "Ibn Sino"
    assert prof2["stats"]["total_games"] == 0
    assert prof2["stats"]["total_score"] == 0
    assert len(prof2["recent_games"]) == 0
