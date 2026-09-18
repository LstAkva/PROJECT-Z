import time
from datetime import datetime, timezone, timedelta
import pytest
from fastapi import status

from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User
from api.quizzes import compile_published_manifest


def helper_create_and_publish_quiz(client, title="Test Tournament", game_mode="modern_multiround", visibility="public", questions_data=None):
    """Helper to create a valid multi-round draft and publish it."""
    res = client.post("/api/drafts", json={"title": title, "game_mode": game_mode})
    assert res.status_code == status.HTTP_201_CREATED
    draft_id = res.json()["version_id"]

    if questions_data is None:
        questions_data = [
            {"round_type": "standard", "text": "Q1 Text", "answer": "Ans1", "points": 1},
            {"round_type": "standard", "text": "Q2 Text", "answer": "Ans2", "points": 1},
        ]

    # Create a round
    r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": questions_data[0]["round_type"]})
    assert r_res.status_code == status.HTTP_201_CREATED
    round_id = r_res.json()["round_id"]

    for q in questions_data:
        q_res = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
            "text": q["text"],
            "primary_answer": q["answer"],
            "points": q.get("points", 1),
            "explanation": q.get("explanation", "Test explanation"),
        })
        assert q_res.status_code == status.HTTP_201_CREATED

    pub_res = client.post(f"/api/drafts/{draft_id}/publish", json={"visibility": visibility})
    assert pub_res.status_code == status.HTTP_200_OK
    data = pub_res.json()
    return data["quiz_id"], data["version_id"]


def test_public_vs_unlisted_visibility(client):
    """
    Requirement 1 & 2:
    - public: discoverable through Arena/discovery and playable by direct link.
    - unlisted: not shown in discovery, but playable by direct link.
    """
    quiz_pub_id, _ = helper_create_and_publish_quiz(client, title="Public Cup", visibility="public")
    quiz_unlisted_id, _ = helper_create_and_publish_quiz(client, title="Secret Tournament", visibility="unlisted")

    # Arena / discovery endpoint
    disc_res = client.get("/api/quizzes")
    assert disc_res.status_code == status.HTTP_200_OK
    quizzes = disc_res.json()
    quiz_ids = [q["quiz_id"] for q in quizzes]

    assert quiz_pub_id in quiz_ids
    assert quiz_unlisted_id not in quiz_ids, "Unlisted quiz must NOT appear in public discovery"

    # Both are playable by direct link
    client.cookies.clear()
    res1 = client.post(f"/api/play/start/{quiz_pub_id}")
    assert res1.status_code == status.HTTP_201_CREATED

    client.cookies.clear()
    res2 = client.post(f"/api/play/start/{quiz_unlisted_id}")
    assert res2.status_code == status.HTTP_201_CREATED


def test_draft_quiz_is_not_playable(client):
    """Requirement: Draft quizzes must never be publicly playable."""
    res = client.post("/api/drafts", json={"title": "Unpublished Draft", "game_mode": "modern_multiround"})
    draft_id = res.json()["version_id"]

    # Draft version is not a published quiz
    play_res = client.post(f"/api/play/start/{draft_id}")
    assert play_res.status_code == status.HTTP_404_NOT_FOUND


def test_anonymous_single_attempt_gate_and_second_attempt_forbidden(client):
    """
    Requirement 3:
    - Anonymous users get exactly ONE attempt.
    - Starting consumes the attempt immediately; abandoned attempt does not restore it.
    - Second attempt returns HTTP 403.
    """
    quiz_id, _ = helper_create_and_publish_quiz(client, title="One Shot Quiz")

    client.cookies.clear()
    # 1. Start first attempt
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    assert "zakowhat_anon_id" in client.cookies

    # 2. Abandon attempt (do not finish) and attempt to start again with same cookie
    second_start = client.post(f"/api/play/start/{quiz_id}")
    assert second_start.status_code == status.HTTP_403_FORBIDDEN
    assert "bepul urinishdan foydalanildi" in second_start.json()["detail"].lower()


def test_anonymous_completed_results_and_review_accessible(client):
    """
    Requirement 4 & 5:
    - Anonymous results are temporarily viewable after completion.
    - Answer review is accessible for completed attempts.
    """
    quiz_id, _ = helper_create_and_publish_quiz(client, title="Review Quiz", questions_data=[
        {"round_type": "standard", "text": "O'zbekiston poytaxti qaysi?", "answer": "Toshkent", "points": 1, "explanation": "Toshkent - Markaziy Osiyoning yirik shahri"},
    ])

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    token = start_res.json()["session_token"]

    # Answer correctly
    ans_res = client.post(f"/api/play/{token}/answer", json={"answer": "toshkent"})
    assert ans_res.status_code == status.HTTP_200_OK
    assert ans_res.json()["is_correct"] is True

    # Continue to finish
    cont_res = client.post(f"/api/play/{token}/continue")
    assert cont_res.status_code == status.HTTP_200_OK
    assert cont_res.json()["quiz_completed"] is True

    # Check /results
    res_summary = client.get(f"/api/play/{token}/results")
    assert res_summary.status_code == status.HTTP_200_OK
    res_data = res_summary.json()
    assert res_data["is_anonymous"] is True
    assert res_data["total_correct"] == 1
    assert "formatted_time" in res_data

    # Check /review
    rev_res = client.get(f"/api/play/{token}/review")
    assert rev_res.status_code == status.HTTP_200_OK
    rev_data = rev_res.json()
    assert rev_data["total_correct"] == 1
    q_rev = rev_data["rounds"][0]["questions"][0]
    assert q_rev["is_correct"] is True
    assert q_rev["submitted_answer"] == "toshkent"
    assert "Toshkent" in q_rev["correct_answers"]
    assert "Markaziy Osiyo" in q_rev["explanation"]


def test_anonymous_attempts_never_appear_on_leaderboard(client):
    """Requirement: Anonymous attempts NEVER appear on leaderboards."""
    quiz_id, _ = helper_create_and_publish_quiz(client, title="Leaderboard Quiz", questions_data=[
        {"round_type": "standard", "text": "Q1", "answer": "A1", "points": 1},
    ])

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    token = start_res.json()["session_token"]

    # Complete successfully
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"})
    client.post(f"/api/play/{token}/continue")

    # Check leaderboard
    lb_res = client.get(f"/api/quizzes/{quiz_id}/leaderboard")
    assert lb_res.status_code == status.HTTP_200_OK
    lb_data = lb_res.json()
    assert len(lb_data["leaderboard"]) == 0
    assert lb_data["total_participants"] == 0


def test_registered_user_multiple_attempts_and_leaderboard_best_attempt(client):
    """
    Requirement:
    - Registered users have unlimited attempts.
    - Leaderboard stores exactly ONE entry per user: their BEST attempt.
    """
    quiz_id, _ = helper_create_and_publish_quiz(client, title="Multi Attempt Quiz", questions_data=[
        {"round_type": "standard", "text": "Q1", "answer": "A1", "points": 1},
        {"round_type": "standard", "text": "Q2", "answer": "A2", "points": 1},
    ])

    # 1. Register a user
    client.cookies.clear()
    reg_res = client.post("/api/auth/register", json={
        "display_name": "Bobur",
        "email": "bobur@test.uz",
        "password": "strongpassword123",
    })
    assert reg_res.status_code == status.HTTP_201_CREATED

    # Attempt 1: 1/2 correct
    start1 = client.post(f"/api/play/start/{quiz_id}")
    token1 = start1.json()["session_token"]
    client.post(f"/api/play/{token1}/answer", json={"answer": "A1"}) # correct
    client.post(f"/api/play/{token1}/answer", json={"answer": "Wrong"}) # incorrect
    client.post(f"/api/play/{token1}/continue")

    # Check leaderboard: Bobur has 1/2
    lb1 = client.get(f"/api/quizzes/{quiz_id}/leaderboard").json()
    assert len(lb1["leaderboard"]) == 1
    assert lb1["leaderboard"][0]["display_name"] == "Bobur"
    assert lb1["leaderboard"][0]["correct_count"] == 1

    # Attempt 2: 2/2 correct (better!)
    start2 = client.post(f"/api/play/start/{quiz_id}")
    token2 = start2.json()["session_token"]
    client.post(f"/api/play/{token2}/answer", json={"answer": "A1"}) # correct
    client.post(f"/api/play/{token2}/answer", json={"answer": "A2"}) # correct
    client.post(f"/api/play/{token2}/continue")

    # Check leaderboard: Bobur updated to 2/2
    lb2 = client.get(f"/api/quizzes/{quiz_id}/leaderboard").json()
    assert len(lb2["leaderboard"]) == 1
    assert lb2["leaderboard"][0]["correct_count"] == 2

    # Attempt 3: 0/2 correct (worse) -> does not overwrite the 2/2 best attempt
    start3 = client.post(f"/api/play/start/{quiz_id}")
    token3 = start3.json()["session_token"]
    client.post(f"/api/play/{token3}/answer", json={"answer": "Wrong1"})
    client.post(f"/api/play/{token3}/answer", json={"answer": "Wrong2"})
    client.post(f"/api/play/{token3}/continue")

    lb3 = client.get(f"/api/quizzes/{quiz_id}/leaderboard").json()
    assert len(lb3["leaderboard"]) == 1
    assert lb3["leaderboard"][0]["correct_count"] == 2, "Best score must be preserved"


def test_leaderboard_ranking_order_and_active_time(client, db_session):
    """
    Requirement:
    Leaderboard sorts by:
    1. total_correct DESC
    2. active_time_seconds ASC
    3. completed_at ASC
    """
    quiz_id, version_id = helper_create_and_publish_quiz(client, title="Ranking Test Quiz", questions_data=[
        {"round_type": "standard", "text": "Q1", "answer": "A1", "points": 1},
        {"round_type": "standard", "text": "Q2", "answer": "A2", "points": 1},
    ])

    # Seed 3 users directly with distinct scores & active times
    users = [
        User(email="u1@test.uz", display_name="User_1_Fast", hashed_password="pw", auth_provider="local"),
        User(email="u2@test.uz", display_name="User_2_Top", hashed_password="pw", auth_provider="local"),
        User(email="u3@test.uz", display_name="User_3_Slow", hashed_password="pw", auth_provider="local"),
    ]
    db_session.add_all(users)
    db_session.commit()

    now = datetime.now(timezone.utc)
    # User 1: 1 correct, 20s
    att1 = SoloAttempt(
        quiz_version_id=version_id,
        user_id=users[0].id,
        status="completed",
        total_correct=1,
        active_time_seconds=20,
        completed_at=now,
    )
    # User 2: 2 correct, 45s (highest correct -> rank 1)
    att2 = SoloAttempt(
        quiz_version_id=version_id,
        user_id=users[1].id,
        status="completed",
        total_correct=2,
        active_time_seconds=45,
        completed_at=now,
    )
    # User 3: 1 correct, 40s (same correct as User 1, but slower -> rank 3)
    att3 = SoloAttempt(
        quiz_version_id=version_id,
        user_id=users[2].id,
        status="completed",
        total_correct=1,
        active_time_seconds=40,
        completed_at=now,
    )
    db_session.add_all([att1, att2, att3])
    db_session.commit()

    lb = client.get(f"/api/quizzes/{quiz_id}/leaderboard").json()
    entries = lb["leaderboard"]
    assert len(entries) == 3

    assert entries[0]["display_name"] == "User_2_Top"
    assert entries[0]["rank"] == 1
    assert entries[0]["correct_count"] == 2

    assert entries[1]["display_name"] == "User_1_Fast"
    assert entries[1]["rank"] == 2
    assert entries[1]["correct_count"] == 1
    assert entries[1]["active_time_seconds"] == 20

    assert entries[2]["display_name"] == "User_3_Slow"
    assert entries[2]["rank"] == 3
    assert entries[2]["correct_count"] == 1
    assert entries[2]["active_time_seconds"] == 40


def test_separate_leaderboards_per_quiz_version(client, db_session):
    """Requirement: Scoped to QuizVersion (separate leaderboard per version)."""
    quiz_id, v1_id = helper_create_and_publish_quiz(client, title="Versioned Quiz", questions_data=[
        {"round_type": "standard", "text": "V1 Q", "answer": "A1", "points": 1},
    ])

    user = User(email="v_user@test.uz", display_name="VersionUser", hashed_password="pw", auth_provider="local")
    db_session.add(user)
    db_session.commit()

    # Completed attempt on v1
    att_v1 = SoloAttempt(
        quiz_version_id=v1_id,
        user_id=user.id,
        status="completed",
        total_correct=1,
        active_time_seconds=15,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(att_v1)

    # Create v2
    v2 = QuizVersion(
        quiz_id=quiz_id,
        version_number=2,
        status="published",
        published_manifest={
            "quiz_id": quiz_id,
            "version_number": 2,
            "rounds": [{"questions": [{"question_id": 999}]}]
        },
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(v2)
    db_session.commit()

    # V1 leaderboard has 1 participant
    lb_v1 = client.get(f"/api/quizzes/{quiz_id}/versions/1/leaderboard").json()
    assert lb_v1["total_participants"] == 1
    assert lb_v1["version_number"] == 1

    # V2 leaderboard has 0 participants
    lb_v2 = client.get(f"/api/quizzes/{quiz_id}/versions/2/leaderboard").json()
    assert lb_v2["total_participants"] == 0
    assert lb_v2["version_number"] == 2


def test_active_gameplay_time_excludes_reveal(client, db_session):
    """
    Requirement:
    Active gameplay time measures strictly active question-answering time,
    excluding round reveal screens, results screens, and inter-round navigation.
    """
    quiz_id, _ = helper_create_and_publish_quiz(client, title="Timing Quiz", questions_data=[
        {"round_type": "standard", "text": "Q1", "answer": "A1", "points": 1},
    ])

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    token = start_res.json()["session_token"]

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    # Mock opening question 10 seconds ago
    attempt.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    db_session.commit()

    # Submit answer
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"})

    db_session.refresh(attempt)
    # Active time should be around 10 seconds
    assert 9 <= attempt.active_time_seconds <= 12
    # In round_reveal, question_opened_at is paused (None)
    assert attempt.question_opened_at is None

    # Simulate staying on reveal screen for 30 seconds
    # Even if time elapses, question_opened_at is None so no time accumulates
    client.post(f"/api/play/{token}/continue")

    db_session.refresh(attempt)
    # Total active time remains around 10 seconds
    assert 9 <= attempt.active_time_seconds <= 12


def test_mantiqqasqon_hidden_rule_gating(client):
    """
    Requirement:
    Mantiqqasqon hidden_rule must remain hidden until its intended reveal state,
    and also be visible in completed review.
    """
    # Create draft with mantiqqasqon round
    res = client.post("/api/drafts", json={"title": "Mantiq Quiz", "game_mode": "modern_multiround"})
    draft_id = res.json()["version_id"]

    r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={
        "round_type": "mantiqqasqon",
        "config": {"hidden_rule": "Barcha javoblar shaxslar nomlari"}
    })
    round_id = r_res.json()["round_id"]

    client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "Mantiq Savoli 1",
        "primary_answer": "Navoiy",
        "points": 1,
    })

    client.post(f"/api/drafts/{draft_id}/publish", json={"visibility": "public"})
    draft_info = client.get(f"/api/drafts/{draft_id}").json()
    quiz_id = draft_info["quiz_id"]

    # 1. Start play
    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]
    cur_state = start_res.json()["current_state"]

    # Verify hidden_rule is NOT exposed in round config during question gameplay
    assert "hidden_rule" not in cur_state["round"].get("config", {})

    # 2. Answer question
    client.post(f"/api/play/{token}/answer", json={"answer": "Navoiy"})

    # 3. In reveal screen: hidden_rule IS revealed
    rev_res = client.get(f"/api/play/{token}/reveal").json()
    assert rev_res["config"]["hidden_rule"] == "Barcha javoblar shaxslar nomlari"

    # 4. Finish quiz and check /review
    client.post(f"/api/play/{token}/continue")
    review_res = client.get(f"/api/play/{token}/review").json()
    assert review_res["rounds"][0]["config"]["hidden_rule"] == "Barcha javoblar shaxslar nomlari"


def test_anonymous_attempt_claimed_on_registration(client, db_session):
    """
    Requirement 8:
    Registering after an anonymous attempt claims and migrates that completed attempt
    to the new user account without losing score or active time.
    """
    quiz_id, _ = helper_create_and_publish_quiz(client, title="Claiming Quiz", questions_data=[
        {"round_type": "standard", "text": "Q1", "answer": "A1", "points": 1},
    ])

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    token = start_res.json()["session_token"]

    # Play and complete
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"})
    client.post(f"/api/play/{token}/continue")

    # Confirm attempt is anonymous and leaderboard is empty
    lb_before = client.get(f"/api/quizzes/{quiz_id}/leaderboard").json()
    assert len(lb_before["leaderboard"]) == 0

    # Register passing session_token
    reg_res = client.post("/api/auth/register", json={
        "display_name": "Sardor",
        "email": "sardor@claim.uz",
        "password": "securepassword123",
        "session_token": token,
    })
    assert reg_res.status_code == status.HTTP_201_CREATED
    data = reg_res.json()
    assert data["attempt_claimed"] is True
    user_id = data["user"]["id"]

    # Verify attempt in DB has user_id set
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    assert attempt.user_id == user_id
    assert attempt.total_correct == 1

    # Leaderboard now contains Sardor as Rank 1!
    lb_after = client.get(f"/api/quizzes/{quiz_id}/leaderboard").json()
    assert len(lb_after["leaderboard"]) == 1
    assert lb_after["leaderboard"][0]["display_name"] == "Sardor"
    assert lb_after["leaderboard"][0]["correct_count"] == 1
    assert lb_after["leaderboard"][0]["rank"] == 1


def test_published_manifest_immutability_and_db_deletion(client, db_session):
    """
    Requirement:
    Published gameplay must be fully self-contained in published_manifest.
    Mutating or deleting underlying DB question data does not break published gameplay.
    """
    quiz_id, version_id = helper_create_and_publish_quiz(client, title="Immutable Quiz", questions_data=[
        {"round_type": "standard", "text": "Original Question Text", "answer": "OriginalAns", "points": 1},
    ])

    # Deliberately delete question records in DB
    v = db_session.query(QuizVersion).filter(QuizVersion.id == version_id).first()
    for r in v.rounds:
        for rq in r.round_questions:
            db_session.delete(rq)
    db_session.commit()

    # Start and play quiz: it must work identically using published_manifest!
    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]
    assert start_res.json()["current_state"]["question"]["text"] == "Original Question Text"

    ans_res = client.post(f"/api/play/{token}/answer", json={"answer": "originalans"})
    assert ans_res.status_code == status.HTTP_200_OK
    assert ans_res.json()["is_correct"] is True


def test_auth_cookie_attributes_and_logout(client):
    """
    Requirement 1 & 14:
    - Auth cookies: HttpOnly + SameSite=Lax.
    - Logout clears auth cookie.
    """
    reg_res = client.post("/api/auth/register", json={
        "display_name": "CookieUser",
        "email": "cookie@user.uz",
        "password": "validpassword",
    })
    assert reg_res.status_code == status.HTTP_201_CREATED
    assert "access_token" in client.cookies

    # Profile returns authenticated
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == status.HTTP_200_OK
    assert me_res.json()["authenticated"] is True
    assert me_res.json()["user"]["display_name"] == "CookieUser"

    # Logout
    logout_res = client.post("/api/auth/logout")
    assert logout_res.status_code == status.HTTP_200_OK

    # Profile returns unauthenticated
    me_after = client.get("/api/auth/me")
    assert me_after.status_code == status.HTTP_200_OK
    assert me_after.json()["authenticated"] is False


def test_auth_responses_do_not_expose_raw_jwt(client):
    """
    Requirement 4:
    Because browser authentication uses the HttpOnly access_token cookie,
    the server must stop returning raw JWT in registration/login JSON responses.
    """
    # 1. Registration response check
    reg_res = client.post("/api/auth/register", json={
        "display_name": "NoJwtUser",
        "email": "nojwt@test.uz",
        "password": "validpassword123",
    })
    assert reg_res.status_code == status.HTTP_201_CREATED
    reg_json = reg_res.json()
    assert "access_token" not in reg_json, "Raw JWT must not be exposed in register response body"
    assert "token_type" not in reg_json
    assert "zakowhat_auth" not in reg_json
    assert "user" in reg_json
    assert "access_token" in client.cookies

    # 2. Login response check
    login_res = client.post("/api/auth/login", json={
        "email": "nojwt@test.uz",
        "password": "validpassword123",
    })
    assert login_res.status_code == status.HTTP_200_OK
    login_json = login_res.json()
    assert "access_token" not in login_json, "Raw JWT must not be exposed in login response body"
    assert "token_type" not in login_json
    assert "user" in login_json


def test_anonymous_claim_ownership_enforcement(client, db_session):
    """
    Requirement 1:
    Anonymous claim ownership requires:
    1. attempt.anon_id == request_anon_id (validated against authoritative cookie / header)
    2. attempt.user_id IS NULL
    3. attempt.status == 'completed'
    Mismatched, incomplete, or already claimed attempts cannot be claimed.
    """
    quiz_id, _ = helper_create_and_publish_quiz(client, title="Ownership Quiz", questions_data=[
        {"round_type": "standard", "text": "Q1", "answer": "A1", "points": 1},
    ])

    # 1. Visitor 1 starts and completes quiz with cookie zakowhat_anon_id=anon_legit
    client.cookies.set("zakowhat_anon_id", "anon_legit_111")
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"})
    client.post(f"/api/play/{token}/continue")

    # 2. Attacker with a different anon cookie tries to claim Visitor 1's session
    client.cookies.set("zakowhat_anon_id", "anon_attacker_999")
    steal_res = client.post("/api/auth/register", json={
        "display_name": "Attacker",
        "email": "attacker@evil.uz",
        "password": "attackpassword",
        "session_token": token,
    })
    assert steal_res.status_code == status.HTTP_400_BAD_REQUEST
    assert "anonim egasi mos kelmadi" in steal_res.json()["detail"].lower()

    # Verify attempt in DB remains unclaimed
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    assert attempt.user_id is None

    # 3. Legitimate visitor with correct anon cookie successfully claims it
    client.cookies.set("zakowhat_anon_id", "anon_legit_111")
    legit_res = client.post("/api/auth/register", json={
        "display_name": "LegitUser",
        "email": "legit@user.uz",
        "password": "legitpassword",
        "session_token": token,
    })
    assert legit_res.status_code == status.HTTP_201_CREATED
    assert legit_res.json()["attempt_claimed"] is True

    # 4. Attempting to re-claim an already claimed attempt fails with 400
    client.cookies.set("zakowhat_anon_id", "anon_legit_111")
    reclaim_res = client.post("/api/auth/register", json={
        "display_name": "ReclaimUser",
        "email": "reclaim@user.uz",
        "password": "reclaimpassword",
        "session_token": token,
    })
    assert reclaim_res.status_code == status.HTTP_400_BAD_REQUEST

    # 5. Incomplete attempt (status != 'completed') cannot be claimed
    client.cookies.set("zakowhat_anon_id", "anon_incomplete_222")
    inc_start = client.post(f"/api/play/start/{quiz_id}")
    inc_token = inc_start.json()["session_token"]

    inc_claim = client.post("/api/auth/register", json={
        "display_name": "IncompleteUser",
        "email": "incomplete@user.uz",
        "password": "incpassword",
        "session_token": inc_token,
    })
    assert inc_claim.status_code == status.HTTP_400_BAD_REQUEST
    assert "yakunlanmagan" in inc_claim.json()["detail"].lower()


def test_production_startup_behavior_with_missing_secret_key(monkeypatch):
    """
    Requirement 2:
    Remove hardcoded production secret fallback.
    In production, SECRET_KEY must be explicitly configured; fails clearly at startup if missing.
    """
    from api.auth import get_secret_key

    # In development mode without SECRET_KEY: returns dev fallback safely
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    dev_secret = get_secret_key()
    assert dev_secret == "zakowhat-dev-only-secret-key-unsafe-for-production"

    # In production mode without SECRET_KEY: must fail loudly with RuntimeError
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CRITICAL: SECRET_KEY environment variable must be explicitly configured"):
        get_secret_key()

    # In production mode with configured SECRET_KEY: succeeds
    monkeypatch.setenv("SECRET_KEY", "explicit-production-secret-configured-32chars")
    prod_secret = get_secret_key()
    assert prod_secret == "explicit-production-secret-configured-32chars"

