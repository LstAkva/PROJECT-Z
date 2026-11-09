import pytest
from datetime import datetime, timezone
from fastapi import status
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User


@pytest.fixture(autouse=True)
def setup_preview_owner_auth(monkeypatch):
    """Preview and publishing lifecycle tests require owner authorization."""
    monkeypatch.setenv("OWNER_EMAIL", "testplayer@zakowhat.uz")


def test_draft_preview_lifecycle_in_player_presentation(client, db_session):
    """
    Priority 1: A draft quiz can be previewed using the actual player-facing gameplay presentation
    without accidentally starting a public production game.
    """
    # 1. Create a draft quiz
    res = client.post("/api/drafts", json={
        "title": "Preview Draft Quiz",
        "description": "Creator testing draft in player UI",
        "game_mode": "modern_multiround",
    })
    assert res.status_code == status.HTTP_201_CREATED
    draft_id = res.json()["version_id"]
    quiz_id = res.json()["quiz_id"]

    # 2. Add round and question
    r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "standard"})
    round_id = r_res.json()["round_id"]

    q_res = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "Alisher Navoiy qaysi asrda yashagan?",
        "primary_answer": "15-asr",
        "accepted_answers": ["15 asr", "XV asr"],
        "explanation": "Alisher Navoiy 1441-1501 yillarda yashagan",
        "points": 2,
    })
    assert q_res.status_code == status.HTTP_201_CREATED

    # 3. Public production game endpoint MUST reject the draft with 404
    public_play = client.post(f"/api/play/start/{quiz_id}")
    assert public_play.status_code == status.HTTP_404_NOT_FOUND

    # 4. Public catalog MUST NOT show the draft quiz
    public_catalog = client.get("/api/quizzes").json()
    assert all(q["quiz_id"] != quiz_id for q in public_catalog)

    # 5. Start isolated preview simulation session
    prev_start = client.post(f"/api/drafts/{draft_id}/preview/start")
    assert prev_start.status_code == status.HTTP_201_CREATED
    prev_data = prev_start.json()

    assert prev_data["is_preview"] is True
    assert prev_data["quiz_id"] == quiz_id
    assert prev_data["version_id"] == draft_id
    assert "session_token" in prev_data
    token = prev_data["session_token"]

    # 6. Verify player UI restore endpoint (GET /api/play/{token}) restores active preview state
    state_res = client.get(f"/api/play/{token}")
    assert state_res.status_code == status.HTTP_200_OK
    state_data = state_res.json()

    assert state_data["status"] == "in_progress"
    assert state_data["is_staging"] is True
    assert state_data["question"]["text"] == "Alisher Navoiy qaysi asrda yashagan?"
    assert state_data["question"]["points"] == 2
    # Ensure answers and explanations NEVER leak before reveal
    assert "primary_answer" not in state_data["question"]
    assert "accepted_answers" not in state_data["question"]
    assert "explanation" not in state_data["question"]

    # 7. Play the preview: submit correct answer
    ans_res = client.post(f"/api/play/{token}/answer", json={"answer": "15-asr"})
    assert ans_res.status_code == status.HTTP_200_OK
    ans_data = ans_res.json()
    assert ans_data["is_correct"] is True
    assert ans_data["points_awarded"] == 2
    assert ans_data["total_score"] == 2
    assert ans_data["round_completed"] is True

    # 8. Round reveal: explanations and answers become available
    rev_res = client.get(f"/api/play/{token}/reveal")
    assert rev_res.status_code == status.HTTP_200_OK
    rev_data = rev_res.json()
    assert "1441-1501" in rev_data["questions"][0]["explanation"]

    # 9. Continue to finalize quiz
    cont_res = client.post(f"/api/play/{token}/continue")
    assert cont_res.status_code == status.HTTP_200_OK
    assert cont_res.json()["quiz_completed"] is True

    # 10. Results screen reflects is_preview: True
    results_res = client.get(f"/api/play/{token}/results")
    assert results_res.status_code == status.HTTP_200_OK
    res_data = results_res.json()
    assert res_data["status"] == "completed"
    assert res_data["is_preview"] is True
    assert res_data["total_score"] == 2

    # 11. Review screen is accessible
    review_res = client.get(f"/api/play/{token}/review")
    assert review_res.status_code == status.HTTP_200_OK
    assert len(review_res.json()["rounds"][0]["questions"]) == 1


def test_preview_sessions_never_leak_to_leaderboards(client, db_session):
    """
    Priority 1 & 4: Preview attempts (both 'preview' and 'preview_no_timer') must NEVER appear
    on published version leaderboards, even if completed by an authenticated user.
    """
    # Create and publish a quiz
    res = client.post("/api/drafts", json={"title": "Leaderboard Isolation Test Quiz"})
    draft_id = res.json()["version_id"]
    quiz_id = res.json()["quiz_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "standard"}).json()
    client.post(f"/api/drafts/{draft_id}/rounds/{r['round_id']}/questions", json={
        "text": "Savol 1", "primary_answer": "Javob 1", "points": 1,
    })
    pub_res = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_res.status_code == status.HTTP_200_OK

    # Create a user
    user = User(email="preview_tester@zakowhat.uz", display_name="PreviewTester", hashed_password="pw")
    db_session.add(user)
    db_session.commit()

    # Complete a preview session with timer_mode="preview"
    att1 = SoloAttempt(
        quiz_version_id=draft_id,
        user_id=user.id,
        status="completed",
        timer_mode="preview",
        total_correct=1,
        total_score=1,
        active_time_seconds=10,
        completed_at=datetime.now(timezone.utc),
    )
    # Complete another preview session with timer_mode="preview_no_timer"
    att2 = SoloAttempt(
        quiz_version_id=draft_id,
        user_id=user.id,
        status="completed",
        timer_mode="preview_no_timer",
        total_correct=1,
        total_score=1,
        active_time_seconds=20,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([att1, att2])
    db_session.commit()

    # Verify version leaderboard is completely empty
    lb_res = client.get(f"/api/quizzes/{quiz_id}/versions/1/leaderboard")
    assert lb_res.status_code == status.HTTP_200_OK
    assert lb_res.json()["total_participants"] == 0
    assert len(lb_res.json()["leaderboard"]) == 0


def test_validation_gives_actionable_errors_and_blocks_publish(client, db_session):
    """
    Priority 2: Validation gives actionable errors for invalid or incomplete drafts
    and blocks publishing when required conditions are not met.
    """
    res = client.post("/api/drafts", json={"title": "Actionable Validation Quiz", "game_mode": "classic_zakovat"})
    draft_id = res.json()["version_id"]

    # 1. Empty draft validation
    val = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val["valid"] is False
    assert val["is_ready_to_publish"] is False
    assert any(e["code"] == "MIN_ROUNDS" for e in val["structured_errors"])

    # 2. Add round with missing media check
    r1 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zakovat_classic"}).json()
    round1_id = r1["round_id"]

    # Add question referencing media without media_url
    q_res = client.post(f"/api/drafts/{draft_id}/rounds/{round1_id}/questions", json={
        "text": "Ushbu suratda tasvirlangan allomani toping.",
        "primary_answer": "Beruniy",
        "media_url": None,
    })
    assert q_res.status_code == status.HTTP_201_CREATED
    rq1_id = q_res.json()["round_question_id"]

    val2 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val2["valid"] is False
    media_errors = [e for e in val2["structured_errors"] if e["code"] == "MISSING_MEDIA_DEPENDENCY"]
    assert len(media_errors) == 1
    assert media_errors[0]["field"] == "media"

    # Now provide media_url -> MISSING_MEDIA_DEPENDENCY must clear
    client.put(f"/api/drafts/{draft_id}/rounds/{round1_id}/questions/{rq1_id}", json={
        "media_url": "https://example.com/beruniy.jpg",
    })
    val3 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert not any(e["code"] == "MISSING_MEDIA_DEPENDENCY" for e in val3["structured_errors"])

    # Attempt publish must be blocked with HTTP 422
    pub_blocked = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_blocked.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    data = pub_blocked.json()["detail"]
    assert "structured_errors" in data
    assert any(e["code"] == "INVALID_ROUND_COUNT" for e in data["structured_errors"])


def test_publishing_immutability_and_draft_isolation(client, db_session):
    """
    Priority 3: Publishing is atomic and produces an immutable, internally consistent published snapshot.
    Creating a new draft version from an existing quiz does not alter the published snapshot.
    Deleting a draft version does not destroy the published quiz.
    """
    # 1. Create and publish version 1
    res = client.post("/api/drafts", json={"title": "Frozen Master Quiz", "description": "Original description"})
    draft_id = res.json()["version_id"]
    quiz_id = res.json()["quiz_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "standard"}).json()
    q_res = client.post(f"/api/drafts/{draft_id}/rounds/{r['round_id']}/questions", json={
        "text": "Asl savol matni v1",
        "primary_answer": "Asl javob",
        "points": 5,
    }).json()

    pub_v1 = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_v1.status_code == status.HTTP_200_OK
    v1_published_manifest = pub_v1.json()

    # 2. Verify published quiz is in Arena and detail endpoint reads published manifest
    from models import QuizVersion

    def v1_manifest_first_question():
        v1 = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == quiz_id, QuizVersion.version_number == 1).first()
        db_session.refresh(v1)
        return v1.published_manifest["rounds"][0]["questions"][0]

    detail_v1 = client.get(f"/api/quizzes/{quiz_id}").json()
    assert detail_v1["title"] == "Frozen Master Quiz"
    assert detail_v1["version_number"] == 1
    # Frozen text lives in the published snapshot; the public detail never exposes question text
    assert v1_manifest_first_question()["text"] == "Asl savol matni v1"
    assert "text" not in detail_v1["rounds"][0]["questions"][0]

    # 3. Create a new draft version (version 2) cloned from quiz
    v2_res = client.post("/api/drafts", json={"from_quiz_id": quiz_id})
    assert v2_res.status_code == status.HTTP_201_CREATED
    v2_data = v2_res.json()
    assert v2_data["version_number"] == 2
    assert v2_data["status"] == "draft"
    v2_id = v2_data["version_id"]

    # 4. Mutate draft version 2: update question text and points in draft
    v2_detail = client.get(f"/api/drafts/{v2_id}").json()
    v2_round = v2_detail["rounds"][0]
    v2_rq_id = v2_round["questions"][0]["round_question_id"]

    client.put(f"/api/drafts/{v2_id}/rounds/{v2_round['round_id']}/questions/{v2_rq_id}", json={
        "text": "MUTATED DRAFT V2 QUESTION TEXT",
        "points": 10,
    })

    # Also update quiz title in draft container
    client.put(f"/api/drafts/{v2_id}", json={
        "title": "UPDATED TITLE IN DRAFT",
    })

    # 5. VERIFY: Version 1 published public presentation remains 100% UNTOUCHED!
    detail_v1_after = client.get(f"/api/quizzes/{quiz_id}").json()
    assert detail_v1_after["title"] == "Frozen Master Quiz"
    assert detail_v1_after["version_number"] == 1
    assert v1_manifest_first_question()["text"] == "Asl savol matni v1"
    assert "MUTATED DRAFT V2" not in client.get(f"/api/quizzes/{quiz_id}").text
    assert detail_v1_after["rounds"][0]["questions"][0]["points"] == 5

    # 6. Authenticate registered user to allow multiple attempts across test steps
    client.post("/api/auth/register", json={
        "email": "immutability_tester@zakowhat.uz",
        "display_name": "ImmutabilityTester",
        "password": "Password123!",
    })

    # VERIFY: Public gameplay on version 1 uses the original frozen question text and points
    play_res = client.post(f"/api/play/start/{quiz_id}")
    assert play_res.status_code == status.HTTP_201_CREATED
    play_data = play_res.json()
    assert play_data["current_state"]["question"]["text"] == "Asl savol matni v1"
    assert play_data["current_state"]["question"]["points"] == 5

    # 7. Safe draft deletion: deleting version 2 draft MUST NOT delete the published quiz or version 1!
    # Re-authenticate as owner to execute draft deletion
    client.post("/api/auth/login", json={
        "email": "testplayer@zakowhat.uz",
        "password": "TestPassword123!",
    })
    del_res = client.delete(f"/api/drafts/{v2_id}")
    assert del_res.status_code == status.HTTP_200_OK

    # Version 1 must still exist, be published, and be playable!
    detail_v1_final = client.get(f"/api/quizzes/{quiz_id}")
    assert detail_v1_final.status_code == status.HTTP_200_OK
    assert detail_v1_final.json()["title"] == "Frozen Master Quiz"

    play_final = client.post(f"/api/play/start/{quiz_id}")
    assert play_final.status_code == status.HTTP_201_CREATED
