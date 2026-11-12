import re
import pytest
from fastapi import status
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User
from api.auth import hash_password, create_access_token


def create_svoyak_test_game(client, db_session, title="Test Svoyak Game"):
    """Creates and publishes a valid 5-theme x 5-question Svoya Igra game in the test database."""
    quiz = Quiz(title=title, description="Test description")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        status="published",
        game_mode="svoyak",
    )
    db_session.add(version)
    db_session.flush()

    rounds_manifest = []
    points_ladder = [10, 20, 30, 40, 50]

    for r_idx in range(1, 6):
        th_name = f"Mavzu {r_idx}"
        round_obj = Round(
            quiz_version_id=version.id,
            sequence=r_idx,
            round_type="svoyak_theme",
            config={"theme_title": th_name}
        )
        db_session.add(round_obj)
        db_session.flush()

        q_list_manifest = []
        for q_idx, pts in enumerate(points_ladder, start=1):
            q_obj = Question(
                text=f"{th_name} savol {pts}",
                points=pts,
                default_points=pts,
                status="approved",
                explanation=f"{th_name} {pts} izohi",
            )
            db_session.add(q_obj)
            db_session.flush()

            ans = AcceptedAnswer(
                question_id=q_obj.id,
                answer_text=f"Javob {r_idx}_{pts}",
                is_primary=True,
            )
            db_session.add(ans)

            rq = RoundQuestion(
                round_id=round_obj.id,
                question_id=q_obj.id,
                sequence=q_idx,
                points_override=pts,
            )
            db_session.add(rq)
            db_session.flush()

            q_list_manifest.append({
                "question_id": q_obj.id,
                "round_question_id": rq.id,
                "sequence": q_idx,
                "text": q_obj.text,
                "points": pts,
                "primary_answer": ans.answer_text,
                "accepted_answers": [{"answer_text": ans.answer_text, "is_primary": True}],
                "explanation": q_obj.explanation,
            })

        rounds_manifest.append({
            "round_id": round_obj.id,
            "sequence": r_idx,
            "round_type": "svoyak_theme",
            "config": {"theme_title": th_name},
            "questions": q_list_manifest,
        })

    version.published_manifest = {
        "title": title,
        "game_mode": "svoyak",
        "category": "Umumiy",
        "visibility": "public",
        "rounds": rounds_manifest,
    }
    db_session.commit()
    db_session.refresh(quiz)
    return quiz


@pytest.fixture
def svoyak_user(db_session):
    user = User(
        email="svoyak_player@zakowhat.uz",
        display_name="Svoyak Player",
        hashed_password=hash_password("Pass123!"),
        auth_provider="local"
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_svoyak_start_returns_board(client, db_session, svoyak_user):
    """Starting Svoya Igra game returns board selection state with all 5 themes and 25 cells."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Test 1")
    token = create_access_token(svoyak_user.id, svoyak_user.email)

    res = client.post(f"/api/play/start/{quiz.id}", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == status.HTTP_201_CREATED
    data = res.json()

    assert data["game_mode"] == "svoyak"
    assert data["status"] == "board_selection"
    assert "board" in data

    board = data["board"]
    assert len(board["themes"]) == 5
    assert board["total_questions"] == 25
    assert board["total_played"] == 0
    assert board["total_score"] == 0

    first_theme = board["themes"][0]
    assert first_theme["theme_title"] == "Mavzu 1"
    assert len(first_theme["cells"]) == 5
    assert [c["points"] for c in first_theme["cells"]] == [10, 20, 30, 40, 50]
    assert all(c["is_played"] is False for c in first_theme["cells"])


def test_svoyak_select_and_answer_correct(client, db_session, svoyak_user):
    """Selecting question via /select and answering correctly awards positive points."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Test 2")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # Start
    start_res = client.post(f"/api/play/start/{quiz.id}", headers=headers)
    session_token = start_res.json()["session_token"]

    # Select Mavzu 1, 20 points (question_index 1)
    sel_res = client.post(f"/api/play/{session_token}/select", json={"round_index": 0, "question_index": 1}, headers=headers)
    assert sel_res.status_code == status.HTTP_200_OK
    sel_data = sel_res.json()
    assert sel_data["question"]["points"] == 20
    assert "Javob" not in sel_data["question"]["text"]

    # Submit correct answer: "Javob 1_20"
    ans_res = client.post(f"/api/play/{session_token}/answer", json={"answer": "Javob 1_20"}, headers=headers)
    assert ans_res.status_code == status.HTTP_200_OK
    ans_data = ans_res.json()

    assert ans_data["is_correct"] is True
    assert ans_data["points_awarded"] == 20
    assert ans_data["total_score"] == 20
    assert ans_data["status"] == "board_selection"

    # Verify board reflects played status
    board_res = client.get(f"/api/play/{session_token}/board", headers=headers)
    assert board_res.status_code == status.HTTP_200_OK
    b_data = board_res.json()["board"]
    assert b_data["total_played"] == 1
    assert b_data["total_score"] == 20
    target_cell = b_data["themes"][0]["cells"][1]
    assert target_cell["is_played"] is True
    assert target_cell["is_correct"] is True
    assert target_cell["points_awarded"] == 20


def test_svoyak_select_and_answer_incorrect_deducts_points(client, db_session, svoyak_user):
    """Answering incorrectly deducts question value (-points)."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Test 3")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", headers=headers)
    session_token = start_res.json()["session_token"]

    # Select Mavzu 2, 40 points (round_index 1, question_index 3)
    client.post(f"/api/play/{session_token}/select", json={"round_index": 1, "question_index": 3}, headers=headers)

    # Submit wrong answer
    ans_res = client.post(f"/api/play/{session_token}/answer", json={"answer": "Noto'g'ri javob"}, headers=headers)
    assert ans_res.status_code == status.HTTP_200_OK
    ans_data = ans_res.json()

    assert ans_data["is_correct"] is False
    assert ans_data["points_awarded"] == -40
    assert ans_data["total_score"] == -40


def test_svoyak_cannot_select_already_played_question(client, db_session, svoyak_user):
    """Selecting an already played question is rejected with HTTP 400."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Test 4")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", headers=headers)
    session_token = start_res.json()["session_token"]

    # Play question 0, 0
    client.post(f"/api/play/{session_token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    client.post(f"/api/play/{session_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)

    # Attempt to re-select the exact same question
    re_sel = client.post(f"/api/play/{session_token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    assert re_sel.status_code == status.HTTP_400_BAD_REQUEST
    assert "allaqachon o'ynalgan" in re_sel.json()["detail"]


def test_svoyak_completion_when_all_questions_played(client, db_session, svoyak_user):
    """When all questions across all themes are played, attempt completes."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Test 5")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", headers=headers)
    session_token = start_res.json()["session_token"]

    points = [10, 20, 30, 40, 50]
    for r_idx in range(5):
        for q_idx in range(5):
            client.post(f"/api/play/{session_token}/select", json={"round_index": r_idx, "question_index": q_idx}, headers=headers)
            res = client.post(f"/api/play/{session_token}/answer", json={"answer": f"Javob {r_idx+1}_{points[q_idx]}"}, headers=headers)

    assert res.json()["quiz_completed"] is True
    assert res.json()["status"] == "completed"

    # Verify attempt status in DB
    att = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    assert att.status == "completed"
    assert att.total_correct == 25
    assert att.total_score == sum(points) * 5  # (10+20+30+40+50) * 5 = 150 * 5 = 750


def test_svoyak_blank_answer_awards_zero_points(client, db_session, svoyak_user):
    """Submitting empty answer or pass in Svoya Igra awards 0 points."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Test 6")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", headers=headers)
    session_token = start_res.json()["session_token"]

    client.post(f"/api/play/{session_token}/select", json={"round_index": 0, "question_index": 2}, headers=headers)
    res = client.post(f"/api/play/{session_token}/answer", json={"answer": ""}, headers=headers)
    assert res.status_code == status.HTTP_200_OK
    assert res.json()["points_awarded"] == 0
    assert res.json()["total_score"] == 0
    assert res.json()["is_correct"] is False


def test_svoyak_arena_catalog_discovery_and_filtering(client, db_session):
    """Svoya Igra games appear in Arena and can be filtered by game_mode=svoyak."""
    create_svoyak_test_game(client, db_session, "Svoyak Discovery Game")

    # All quizzes
    res_all = client.get("/api/quizzes")
    assert res_all.status_code == status.HTTP_200_OK
    items_all = res_all.json()
    assert any(q["game_mode"] == "svoyak" and q["title"] == "Svoyak Discovery Game" for q in items_all)

    # Filtered by svoyak
    res_svoyak = client.get("/api/quizzes?game_mode=svoyak")
    assert res_svoyak.status_code == status.HTTP_200_OK
    items_svoyak = res_svoyak.json()
    assert len(items_svoyak) >= 1
    assert all(q["game_mode"] == "svoyak" for q in items_svoyak)

    # Filtered by classic_zakovat excludes svoyak
    res_classic = client.get("/api/quizzes?game_mode=classic_zakovat")
    assert res_classic.status_code == status.HTTP_200_OK
    items_classic = res_classic.json()
    assert not any(q["game_mode"] == "svoyak" for q in items_classic)


def test_svoyak_frontend_board_dom_rendering():
    """Verify that templates/index.html includes svoyak board container and renderSvoyakBoard works."""
    selenium = pytest.importorskip("selenium")
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)

    try:
        import os
        index_path = os.path.abspath("templates/index.html")
        driver.get(f"file:///{index_path.replace(os.sep, '/')}")

        # Verify initial DOM elements exist
        board_container = driver.find_element("id", "svoyak-board-container")
        assert board_container is not None

        # Execute renderSvoyakBoard with mock data
        mock_board = {
            "total_questions": 25,
            "total_played": 2,
            "total_score": 30,
            "themes": [
                {
                    "theme_title": "Toshkent",
                    "cells": [
                        {"points": 10, "is_played": True, "is_correct": True, "points_awarded": 10},
                        {"points": 20, "is_played": True, "is_correct": False, "points_awarded": -20},
                        {"points": 30, "is_played": False},
                        {"points": 40, "is_played": False},
                        {"points": 50, "is_played": False},
                    ]
                },
                {
                    "theme_title": "Samarqand",
                    "cells": [
                        {"points": 10, "is_played": False},
                        {"points": 20, "is_played": False},
                        {"points": 30, "is_played": False},
                        {"points": 40, "is_played": False},
                        {"points": 50, "is_played": False},
                    ]
                }
            ]
        }

        res = driver.execute_script("""
            var board = arguments[0];
            try {
                renderSvoyakBoard(board);
                var container = document.getElementById('svoyak-board-container');
                var isHidden = container.classList.contains('hidden');
                var scoreText = document.getElementById('svoyak-board-score-pill').innerText;
                var rows = document.getElementById('svoyak-grid-rows').children.length;
                return { ok: true, isHidden: isHidden, scoreText: scoreText, rows: rows };
            } catch(e) {
                return { ok: false, error: e.name + ': ' + e.message };
            }
        """, mock_board)

        assert res["ok"] is True, f"JS execution failed: {res.get('error')}"
        assert res["isHidden"] is False
        assert "30" in res["scoreText"]
        assert res["rows"] == 2

    finally:
        driver.quit()



# ==============================================================================
# SVOYA IGRA UX & LOGIC IMPROVEMENTS - TEST SUITE
# ==============================================================================

def test_svoyak_free_choice_allows_selecting_any_available_question(client, db_session, svoyak_user):
    """Requirement 1: Free Choice mode allows freely selecting any available category & question."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Free Choice 1")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # Start with free_choice mode
    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    assert start_res.status_code == status.HTTP_201_CREATED
    data = start_res.json()
    assert data["status"] == "board_selection"
    assert data["svoyak_mode"] == "free_choice"
    session_token = data["session_token"]

    # Select Category 2, 40 points (round_index 1, question_index 3)
    sel1 = client.post(f"/api/play/{session_token}/select", json={"round_index": 1, "question_index": 3}, headers=headers)
    assert sel1.status_code == status.HTTP_200_OK
    assert sel1.json()["question"]["points"] == 40

    # Answer it
    ans1 = client.post(f"/api/play/{session_token}/answer", json={"answer": "Javob 2_40"}, headers=headers)
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["status"] == "board_selection"

    # Select Category 4, 10 points (round_index 3, question_index 0)
    sel2 = client.post(f"/api/play/{session_token}/select", json={"round_index": 3, "question_index": 0}, headers=headers)
    assert sel2.status_code == status.HTTP_200_OK
    assert sel2.json()["question"]["points"] == 10


def test_svoyak_classic_mode_enforces_category_by_category_10_to_50_progression(client, db_session, svoyak_user):
    """Requirement 2: Classic mode strictly enforces category-by-category, 10-to-50 progression."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Classic 1")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # Start with classic mode
    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    assert start_res.status_code == status.HTTP_201_CREATED
    data = start_res.json()
    assert data["status"] == "in_progress"
    assert data["svoyak_mode"] == "classic"
    session_token = data["session_token"]

    # First question is Category 0, 10 points
    assert data["current_state"]["question"]["points"] == 10
    assert data["current_state"]["round"]["sequence"] == 1

    # Each answer shows a reveal; the next question opens on "Davom etish" (/continue)
    def answer_then_continue(answer):
        ans = client.post(f"/api/play/{session_token}/answer", json={"answer": answer}, headers=headers)
        assert ans.status_code == status.HTTP_200_OK
        assert ans.json()["awaiting_continue"] is True
        assert ans.json()["next_state"] is None
        cont = client.post(f"/api/play/{session_token}/continue", headers=headers)
        assert cont.status_code == status.HTTP_200_OK
        return cont.json()["current_state"]

    # Answer Q(0, 0) -> advances to Q(0, 1) = 20 pts
    nxt = answer_then_continue("Javob 1_10")
    assert nxt["question"]["points"] == 20
    assert nxt["round"]["sequence"] == 1

    # Answer Q(0, 1) -> advances to Q(0, 2) = 30 pts
    assert answer_then_continue("Javob 1_20")["question"]["points"] == 30

    # Answer Q(0, 2) -> advances to Q(0, 3) = 40 pts
    assert answer_then_continue("Javob 1_30")["question"]["points"] == 40

    # Answer Q(0, 3) -> advances to Q(0, 4) = 50 pts
    nxt = answer_then_continue("Javob 1_40")
    assert nxt["question"]["points"] == 50
    assert nxt["round"]["sequence"] == 1

    # Answer Q(0, 4) -> advances automatically to Category 1 (Round 2), Question 0 (10 pts)!
    nxt = answer_then_continue("Javob 1_50")
    assert nxt["question"]["points"] == 10
    assert nxt["round"]["sequence"] == 2


def test_svoyak_classic_mode_rejects_out_of_order_or_skip_attempts(client, db_session, svoyak_user):
    """Requirement 3: Classic mode rejects attempts to skip questions or select out of order with HTTP 400."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Classic Skip Rejection")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    session_token = start_res.json()["session_token"]

    # Attempt to select Question 2 (30 pts) instead of the initial Question 0 (10 pts)
    bad_sel1 = client.post(f"/api/play/{session_token}/select", json={"round_index": 0, "question_index": 2}, headers=headers)
    assert bad_sel1.status_code == status.HTTP_400_BAD_REQUEST
    assert "Klassik rejimda savollar ketma-ket o'ynaladi" in bad_sel1.json()["detail"]

    # Attempt to jump to Category 2 (round_index 2)
    bad_sel2 = client.post(f"/api/play/{session_token}/select", json={"round_index": 2, "question_index": 0}, headers=headers)
    assert bad_sel2.status_code == status.HTTP_400_BAD_REQUEST
    assert "Klassik rejimda savollar ketma-ket o'ynaladi" in bad_sel2.json()["detail"]


def test_svoyak_answering_advances_game_without_refreshing(client, db_session, svoyak_user):
    """Requirement 4: Submitting an answer returns immediate next_state or board_available without page reload."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Auto Advance")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # In Classic mode:
    start_c = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    sess_c = start_c.json()["session_token"]
    ans_c = client.post(f"/api/play/{sess_c}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans_c.status_code == status.HTTP_200_OK
    c_data = ans_c.json()
    # Classic: reveal first, then "Davom etish" (/continue) returns the next question without a reload
    assert c_data["awaiting_continue"] is True
    assert c_data["correct_answer"] == "Javob 1_10"
    assert c_data["is_correct"] is True
    assert c_data["total_score"] == 10
    cont_c = client.post(f"/api/play/{sess_c}/continue", headers=headers)
    assert cont_c.status_code == status.HTTP_200_OK
    assert cont_c.json()["current_state"]["question"]["points"] == 20

    # In Free Choice mode:
    quiz_f = create_svoyak_test_game(client, db_session, "Svoyak Auto Advance Free Choice")
    start_f = client.post(f"/api/play/start/{quiz_f.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    sess_f = start_f.json()["session_token"]
    client.post(f"/api/play/{sess_f}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    ans_f = client.post(f"/api/play/{sess_f}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans_f.status_code == status.HTTP_200_OK
    f_data = ans_f.json()
    assert f_data["status"] == "board_selection"
    assert f_data["board_available"] is True
    assert f_data["board"]["total_played"] == 1


def test_svoyak_reloading_restores_correct_progression(client, db_session, svoyak_user):
    """Requirement 5: Reloading restores the correct progression and state without repeating completed questions."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Reload Test")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # Classic mode: answer first two questions
    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    sess_token = start_res.json()["session_token"]
    client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    client.post(f"/api/play/{sess_token}/continue", headers=headers)
    client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_20"}, headers=headers)

    # Refresh DURING the reveal: the reveal is restored, the next question stays hidden
    during = client.get(f"/api/play/{sess_token}", headers=headers).json()
    assert during["awaiting_continue"] is True
    assert during["current_state"] is None
    assert during["last_reveal"]["correct_answer"] == "Javob 1_20"
    client.post(f"/api/play/{sess_token}/continue", headers=headers)

    # Player refreshes / gets current state:
    get_res = client.get(f"/api/play/{sess_token}", headers=headers)
    assert get_res.status_code == status.HTTP_200_OK
    res_data = get_res.json()
    assert res_data["status"] == "in_progress"
    assert res_data["svoyak_mode"] == "classic"
    assert res_data["total_score"] == 30
    # Next question is Q(0, 2) which has points = 30
    assert res_data["current_state"]["question"]["points"] == 30
    assert res_data["current_state"]["round_question_index"] == 2


def test_svoyak_completed_questions_cannot_be_replayed(client, db_session, svoyak_user):
    """Requirement 6: Completed questions cannot be selected or played again."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak No Replay")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    sess_token = start_res.json()["session_token"]

    client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 1}, headers=headers)
    client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_20"}, headers=headers)

    # Attempt to select the exact same question again
    re_sel = client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 1}, headers=headers)
    assert re_sel.status_code == status.HTTP_400_BAD_REQUEST
    assert "allaqachon o'ynalgan" in re_sel.json()["detail"]


def test_svoyak_scores_update_correctly_and_single_source_of_truth(client, db_session, svoyak_user):
    """Requirement 7: Scores update accurately and UI maintains a single clear source of truth."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Scoring Accuracy")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    sess_token = start_res.json()["session_token"]

    # 1. Correct answer (+10)
    client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    ans1 = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans1.json()["total_score"] == 10

    # 2. Incorrect answer (-30)
    client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 2}, headers=headers)
    ans2 = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Wrong Answer"}, headers=headers)
    assert ans2.json()["total_score"] == -20  # 10 - 30 = -20

    # 3. Blank answer (0)
    client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 3}, headers=headers)
    ans3 = client.post(f"/api/play/{sess_token}/answer", json={"answer": ""}, headers=headers)
    assert ans3.json()["total_score"] == -20  # -20 + 0 = -20

    # Verify DOM/template has only one score element on the board (svoyak-board-score-pill) and clock-display is hidden
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "id=\"svoyak-board-score-pill\"" in html
    assert "clockDisplay.classList.add('hidden')" in html


def test_svoyak_completing_last_question_displays_final_result(client, db_session, svoyak_user):
    """Requirement 8: Completing the last question displays the final result and marks attempt completed."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Full Complete")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    sess_token = start_res.json()["session_token"]

    points = [10, 20, 30, 40, 50]
    last_res = None
    for r_idx in range(5):
        for q_idx in range(5):
            pts = points[q_idx]
            last_res = client.post(
                f"/api/play/{sess_token}/answer",
                json={"answer": f"Javob {r_idx+1}_{pts}"},
                headers=headers
            )
            assert last_res.status_code == status.HTTP_200_OK
            if last_res.json().get("awaiting_continue"):  # Classic: reveal, then "Davom etish"
                assert client.post(f"/api/play/{sess_token}/continue", headers=headers).status_code == 200

    assert last_res is not None
    data = last_res.json()
    assert data["quiz_completed"] is True
    assert data["status"] == "completed"
    assert data["total_score"] == 750

    # Query results endpoint
    results_res = client.get(f"/api/play/{sess_token}/results", headers=headers)
    assert results_res.status_code == status.HTTP_200_OK
    res_data = results_res.json()
    assert res_data["status"] == "completed"
    assert res_data["total_score"] == 750


# =====================================================================
# RESTART PACK FUNCTIONALITY TESTS
# =====================================================================

def test_restart_active_free_choice_svoyak(client, db_session, svoyak_user):
    """Scenario 1 & 2: Restarting an active Free Choice Svoyak game resets board, score, and cells."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Free Choice Game")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # Start Free Choice game
    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    assert start_res.status_code == status.HTTP_201_CREATED
    sess_token = start_res.json()["session_token"]

    # Select and answer Question 1 (10 pts) and Question 2 (20 pts)
    sel1 = client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    assert sel1.status_code == status.HTTP_200_OK
    ans1 = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["total_score"] == 10

    sel2 = client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 1}, headers=headers)
    assert sel2.status_code == status.HTTP_200_OK
    ans2 = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_20"}, headers=headers)
    assert ans2.status_code == status.HTTP_200_OK
    assert ans2.json()["total_score"] == 30

    # Call restart endpoint
    restart_res = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert restart_res.status_code == status.HTTP_200_OK
    r_data = restart_res.json()

    assert r_data["restarted"] is True
    assert r_data["total_score"] == 0
    assert r_data["status"] == "board_selection"
    assert r_data["svoyak_mode"] == "free_choice"
    assert r_data["board"]["total_played"] == 0
    assert r_data["board"]["total_questions"] == 25
    # Ranked attempt is never reset in place: restart abandons it and continues on a new (practice) session
    assert r_data["session_token"] != sess_token
    sess_token = r_data["session_token"]

    # Check all cells on board are unplayed
    for theme in r_data["board"]["themes"]:
        for cell in theme["cells"]:
            assert cell["is_played"] is False

    # Check that previously answered question (0, 0) is available and can be answered again
    sel_re = client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    assert sel_re.status_code == status.HTTP_200_OK
    ans_re = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans_re.status_code == status.HTTP_200_OK
    assert ans_re.json()["total_score"] == 10


def test_restart_active_classic_svoyak(client, db_session, svoyak_user):
    """Scenario 3: Restarting Classic Svoyak returns to Category 1, Question 1 (10 pts) and preserves sequential order."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Classic Game")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    assert start_res.status_code == status.HTTP_201_CREATED
    sess_token = start_res.json()["session_token"]

    # Answer 10, 20, 30 pts in Category 1 (each reveal requires "Davom etish")
    client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    client.post(f"/api/play/{sess_token}/continue", headers=headers)
    client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_20"}, headers=headers)
    client.post(f"/api/play/{sess_token}/continue", headers=headers)
    ans3 = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_30"}, headers=headers)
    assert ans3.json()["total_score"] == 60

    # Restart
    restart_res = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert restart_res.status_code == status.HTTP_200_OK
    r_data = restart_res.json()

    assert r_data["restarted"] is True
    assert r_data["total_score"] == 0
    assert r_data["status"] == "in_progress"
    assert r_data["svoyak_mode"] == "classic"
    assert r_data["current_state"]["question"]["points"] == 10
    assert "Mavzu 1" in r_data["current_state"]["question"]["text"]
    # Ranked attempt is never reset in place: continue on the new (practice) session
    assert r_data["session_token"] != sess_token
    sess_token = r_data["session_token"]

    # First question can be answered again and awards 10 points
    ans_first = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans_first.status_code == status.HTTP_200_OK
    assert ans_first.json()["total_score"] == 10


def test_restart_zakovat_gameplay(client, db_session, svoyak_user):
    """Scenario 4: Restarting Zakovat 2x12 quiz returns to Round 1, Question 1 with score 0 and preserves timer mode."""
    # Create Zakovat quiz
    quiz = Quiz(title="Zakovat Restart Test", description="2x12 game")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="classic_zakovat")
    db_session.add(version)
    db_session.flush()

    round_obj = Round(quiz_version_id=version.id, sequence=1, round_type="zakovat_classic", config={"name": "1-tur"})
    db_session.add(round_obj)
    db_session.flush()

    q_manifest = []
    for q_i in range(1, 13):
        q = Question(text=f"Zakovat savol {q_i}", points=1, status="approved")
        db_session.add(q)
        db_session.flush()
        a = AcceptedAnswer(question_id=q.id, answer_text=f"Javob {q_i}", is_primary=True)
        db_session.add(a)
        rq = RoundQuestion(round_id=round_obj.id, question_id=q.id, sequence=q_i)
        db_session.add(rq)
        db_session.flush()
        q_manifest.append({"question_id": q.id, "sequence": q_i, "text": q.text, "primary_answer": a.answer_text, "points": 1, "accepted_answers": [{"answer_text": a.answer_text, "is_primary": True}]})

    version.published_manifest = {"rounds": [{"round_id": round_obj.id, "sequence": 1, "round_type": "zakovat_classic", "config": {"name": "1-tur"}, "questions": q_manifest}]}
    db_session.commit()

    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    # No-timer unlocks only after the timed ranked attempt has been started (option B)
    ranked = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "with_timer"}, headers=headers)
    assert ranked.status_code == status.HTTP_201_CREATED

    # Start a no_timer practice attempt
    start_res = client.post(f"/api/play/start/{quiz.id}", json={"timer_mode": "no_timer", "restart": True}, headers=headers)
    assert start_res.status_code == status.HTTP_201_CREATED
    sess_token = start_res.json()["session_token"]

    # Answer question 1
    ans_res = client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1"}, headers=headers)
    assert ans_res.json()["total_score"] == 1

    # Restart (practice attempt -> reset in place, same session)
    restart_res = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert restart_res.status_code == status.HTTP_200_OK
    r_data = restart_res.json()

    assert r_data["session_token"] == sess_token
    assert r_data["total_score"] == 0
    assert r_data["status"] == "in_progress"
    assert r_data["timer_mode"] == "no_timer"
    assert r_data["current_state"]["round_question_index"] == 0


def test_restart_page_refresh_restores_initial_state(client, db_session, svoyak_user):
    """Scenario 8: Reloading page after restart restores the clean initial state."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Reload Game")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    sess_token = start_res.json()["session_token"]

    # Answer question
    client.post(f"/api/play/{sess_token}/select", json={"round_index": 0, "question_index": 0}, headers=headers)
    client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)

    # Restart (ranked attempt -> abandoned; play continues on the new session)
    new_token = client.post(f"/api/play/{sess_token}/restart", headers=headers).json()["session_token"]
    assert client.get(f"/api/play/{sess_token}", headers=headers).status_code == status.HTTP_410_GONE

    # Simulate page refresh: GET /api/play/{session_token}
    refresh_res = client.get(f"/api/play/{new_token}", headers=headers)
    assert refresh_res.status_code == status.HTTP_200_OK
    data = refresh_res.json()

    assert data["status"] == "board_selection"
    assert data["total_score"] == 0
    assert data["board"]["total_played"] == 0


def test_restart_preserves_completed_historical_results(client, db_session, svoyak_user):
    """Scenario 9: Restarting an already completed game preserves the historical attempt and spawns a new attempt."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Completed Historical")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic"}, headers=headers)
    sess_token = start_res.json()["session_token"]

    # Complete the game
    points = [10, 20, 30, 40, 50]
    for r_idx in range(5):
        for q_idx in range(5):
            pts = points[q_idx]
            res = client.post(f"/api/play/{sess_token}/answer", json={"answer": f"Javob {r_idx+1}_{pts}"}, headers=headers)
            if res.json().get("awaiting_continue"):
                client.post(f"/api/play/{sess_token}/continue", headers=headers)

    # Verify attempt is completed with 750 points
    completed_attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == sess_token).first()
    assert completed_attempt.status == "completed"
    assert completed_attempt.total_score == 750

    # Call restart on the completed session
    restart_res = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert restart_res.status_code == status.HTTP_200_OK
    r_data = restart_res.json()

    # Original historical attempt MUST still exist intact with 750 points!
    db_session.refresh(completed_attempt)
    assert completed_attempt.status == "completed"
    assert completed_attempt.total_score == 750

    # The returned session is a new attempt with score 0
    assert r_data["session_token"] != sess_token
    assert r_data["total_score"] == 0


def test_restart_unauthorized_protection(client, db_session, svoyak_user):
    """Scenario 10: Unauthorized player cannot restart another player's attempt."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Auth Guard Game")
    token_user1 = create_access_token(svoyak_user.id, svoyak_user.email)
    headers_user1 = {"Authorization": f"Bearer {token_user1}"}

    # User 1 starts game
    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers_user1)
    sess_token = start_res.json()["session_token"]

    # User 2 tries to restart User 1's session
    user2 = User(email="intruder@example.com", display_name="Intruder", hashed_password=hash_password("pw123"))
    db_session.add(user2)
    db_session.commit()
    token_user2 = create_access_token(user2.id, user2.email)
    headers_user2 = {"Authorization": f"Bearer {token_user2}"}

    forbidden_res = client.post(f"/api/play/{sess_token}/restart", headers=headers_user2)
    assert forbidden_res.status_code == status.HTTP_403_FORBIDDEN

    # Unauthenticated request (clear cookies so no session persists)
    client.cookies.clear()
    unauth_res = client.post(f"/api/play/{sess_token}/restart")
    assert unauth_res.status_code == status.HTTP_401_UNAUTHORIZED


def test_restart_idempotency(client, db_session, svoyak_user):
    """Scenario 11: Repeated restart calls are idempotent and do not corrupt state."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Idempotency Game")
    token = create_access_token(svoyak_user.id, svoyak_user.email)
    headers = {"Authorization": f"Bearer {token}"}

    start_res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    sess_token = start_res.json()["session_token"]

    # Restart once
    res1 = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert res1.status_code == status.HTTP_200_OK

    # Restart second time immediately
    res2 = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert res2.status_code == status.HTTP_200_OK
    assert res2.json()["total_score"] == 0
    assert res2.json()["status"] == "board_selection"


def test_restart_ui_elements_in_template():
    """Scenario 12: Index template contains restart button and confirmation dialog with exact Uzbek copy."""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    # Restart button in in-game HUD
    assert "confirmRestartGameplay()" in html
    assert "Qaytadan boshlash" in html

    # Confirmation modal with required text
    assert 'id="modal-gameplay-restart"' in html
    assert "Qaytadan boshlaysizmi?" in html
    assert "Joriy o'yindagi barcha natijalar va progress yo'qoladi. Shu to'plamni boshidan boshlashni xohlaysizmi?" in html
    assert "Bekor qilish" in html
    assert "Ha, qaytadan boshlash" in html


# =====================================================================
# RESTART PACK: AUTH, TIMER PRESERVATION, RESTART FROM RESULTS
# =====================================================================

def test_restart_requires_authentication_and_ownership(client, db_session, svoyak_user):
    """Restart rejects anonymous callers (401), other users (403) and ownerless attempts (403)."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Auth Matrix Game")
    headers = {"Authorization": f"Bearer {create_access_token(svoyak_user.id, svoyak_user.email)}"}
    sess_token = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers).json()["session_token"]

    client.cookies.clear()
    assert client.post(f"/api/play/{sess_token}/restart").status_code == status.HTTP_401_UNAUTHORIZED

    other = User(email="other_restarter@example.com", display_name="Other", hashed_password=hash_password("pw123"))
    db_session.add(other)
    db_session.commit()
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id, other.email)}"}
    assert client.post(f"/api/play/{sess_token}/restart", headers=other_headers).status_code == status.HTTP_403_FORBIDDEN

    # A legacy attempt without an owner cannot be restarted by anyone
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == sess_token).first()
    attempt.user_id = None
    db_session.commit()
    assert client.post(f"/api/play/{sess_token}/restart", headers=headers).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("requested, db_mode", [("no_timer", "preview_no_timer"), ("with_timer", "preview")])
def test_restart_preview_session_preserves_timer_mode(client, db_session, requested, db_mode, monkeypatch):
    """Regression: restarting a staging preview keeps its timer mode (preview_no_timer must stay no_timer)."""
    from tests.test_staging_gameplay_api import create_staged_classic_pack, authenticate_editor

    monkeypatch.setenv("STAGING_AUTHORIZED_EMAILS", f"restart_editor_{requested}@zakowhat.uz")  # staging fails closed
    quiz_id, _ = create_staged_classic_pack(client, pack_num=31)
    authenticate_editor(client, f"restart_editor_{requested}@zakowhat.uz")

    start = client.post(f"/api/play/staging/start/{quiz_id}", json={"timer_mode": requested})
    assert start.status_code == status.HTTP_201_CREATED
    sess_token = start.json()["session_token"]
    assert client.post(f"/api/play/{sess_token}/answer", json={"answer": "Javob 1"}).status_code == status.HTTP_200_OK

    res = client.post(f"/api/play/{sess_token}/restart")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["timer_mode"] == requested
    assert data["is_staging"] is True
    assert data["total_score"] == 0
    if requested == "no_timer":
        assert data["current_state"]["question"]["time_limit_seconds"] == 0
    else:
        assert data["current_state"]["question"]["time_limit_seconds"] >= 60

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == data["session_token"]).first()
    db_session.refresh(attempt)
    assert attempt.timer_mode == db_mode
    assert (attempt.attempt_metadata or {}).get("timer_mode") == requested


def test_restart_completed_preview_no_timer_keeps_no_timer(client, db_session, monkeypatch):
    """A completed preview_no_timer attempt restarts into a new preview_no_timer attempt with timers disabled."""
    from tests.test_staging_gameplay_api import create_staged_classic_pack, authenticate_editor

    monkeypatch.setenv("STAGING_AUTHORIZED_EMAILS", "restart_editor_completed@zakowhat.uz")  # staging fails closed
    quiz_id, _ = create_staged_classic_pack(client, pack_num=32)
    authenticate_editor(client, "restart_editor_completed@zakowhat.uz")
    sess_token = client.post(f"/api/play/staging/start/{quiz_id}", json={"timer_mode": "no_timer"}).json()["session_token"]

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == sess_token).first()
    attempt.status = "completed"
    attempt.total_score = 5
    db_session.commit()

    res = client.post(f"/api/play/{sess_token}/restart")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["session_token"] != sess_token
    assert data["timer_mode"] == "no_timer"

    new_attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == data["session_token"]).first()
    assert new_attempt.timer_mode == "preview_no_timer"
    assert new_attempt.attempt_metadata["timer_enabled"] is False
    db_session.refresh(attempt)
    assert attempt.status == "completed"
    assert attempt.total_score == 5


def test_restart_public_timer_modes_preserved(client, db_session, svoyak_user):
    """Public standard and no_timer attempts keep their timer mode across restart."""
    quiz = create_svoyak_test_game(client, db_session, "Restart Public Timer Game")
    headers = {"Authorization": f"Bearer {create_access_token(svoyak_user.id, svoyak_user.email)}"}

    # Timed first (ranked); no_timer practice unlocks only after that (option B)
    for requested, db_mode in (("with_timer", "standard"), ("no_timer", "no_timer")):
        start = client.post(
            f"/api/play/start/{quiz.id}",
            json={"svoyak_mode": "classic", "timer_mode": requested, "restart": True},
            headers=headers,
        )
        assert start.status_code == status.HTTP_201_CREATED
        res = client.post(f"/api/play/{start.json()['session_token']}/restart", headers=headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["timer_mode"] == requested
        attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == data["session_token"]).first()
        db_session.refresh(attempt)
        assert attempt.timer_mode == db_mode


def test_restart_from_results_keeps_history_and_mode(client, db_session, svoyak_user):
    """Restarting a completed pack keeps the old result, reuses pack and mode, and the new run is separate."""
    quiz = create_svoyak_test_game(client, db_session, "Restart From Results Game")
    headers = {"Authorization": f"Bearer {create_access_token(svoyak_user.id, svoyak_user.email)}"}
    sess_token = client.post(
        f"/api/play/start/{quiz.id}", json={"svoyak_mode": "classic", "timer_mode": "with_timer"}, headers=headers
    ).json()["session_token"]

    for r_idx in range(5):
        for pts in (10, 20, 30, 40, 50):
            res = client.post(f"/api/play/{sess_token}/answer", json={"answer": f"Javob {r_idx+1}_{pts}"}, headers=headers)
            if res.json().get("awaiting_continue"):
                client.post(f"/api/play/{sess_token}/continue", headers=headers)
    assert client.get(f"/api/play/{sess_token}/results", headers=headers).json()["total_score"] == 750

    res = client.post(f"/api/play/{sess_token}/restart", headers=headers)
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    new_token = data["session_token"]
    assert new_token != sess_token
    assert data["quiz_id"] == quiz.id
    assert data["game_mode"] == "svoyak"
    assert data["svoyak_mode"] == "classic"
    assert data["timer_mode"] == "with_timer"

    # Playing the new run must not touch the historical result
    ans = client.post(f"/api/play/{new_token}/answer", json={"answer": "Javob 1_10"}, headers=headers)
    assert ans.json()["total_score"] == 10
    old = client.get(f"/api/play/{sess_token}/results", headers=headers).json()
    assert old["status"] == "completed"
    assert old["total_score"] == 750

    old_attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == sess_token).first()
    completed = (
        db_session.query(SoloAttempt)
        .filter(
            SoloAttempt.user_id == svoyak_user.id,
            SoloAttempt.quiz_version_id == old_attempt.quiz_version_id,
            SoloAttempt.status == "completed",
        )
        .all()
    )
    assert [a.session_token for a in completed] == [sess_token]


def test_results_screen_has_restart_button():
    """The completed-game results screen offers restart via the existing confirmation flow."""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    results_start = html.index('id="screen-results"')
    results_end = html.index("</section>", results_start)
    results_html = html[results_start:results_end]
    assert 'id="results-restart-btn"' in results_html
    assert 'onclick="confirmRestartGameplay()"' in results_html
    assert "Qaytadan boshlash" in results_html
    # Restart falls back to the completed session token kept for the results screen
    assert "state.sessionToken || state.lastCompletedSessionToken" in html


# =====================================================================
# SVOYAK PASS MECHANIC & BOARD SCORING DISPLAY
# =====================================================================

def _start_free_choice(client, db_session, svoyak_user, title):
    quiz = create_svoyak_test_game(client, db_session, title)
    headers = {"Authorization": f"Bearer {create_access_token(svoyak_user.id, svoyak_user.email)}"}
    res = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    assert res.status_code == status.HTTP_201_CREATED
    return res.json()["session_token"], headers


def _board_cell(board, r_idx, q_idx):
    return board["themes"][r_idx]["cells"][q_idx]


@pytest.mark.parametrize("pass_text", ["o'tkazish", "o‘tkazish", "Oʻtkazish", "O’TKAZISH.", "pass", "otkazish", ""])
def test_svoyak_pass_scores_zero_for_all_spellings(client, db_session, svoyak_user, pass_text):
    """Pass keywords (including curly/modifier apostrophes) and empty answers score 0, never a penalty."""
    token, headers = _start_free_choice(client, db_session, svoyak_user, f"Pass Spelling {pass_text!r}")
    assert client.post(f"/api/play/{token}/select", json={"round_index": 0, "question_index": 4}, headers=headers).status_code == 200

    res = client.post(f"/api/play/{token}/answer", json={"answer": pass_text}, headers=headers)
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["points_awarded"] == 0
    assert data["total_score"] == 0
    assert data["is_correct"] is False

    cell = _board_cell(data["board"], 0, 4)
    assert cell["is_played"] is True
    assert cell["is_passed"] is True
    assert cell["points_awarded"] == 0


def test_svoyak_wrong_answer_still_subtracts_value(client, db_session, svoyak_user):
    """Existing rule preserved: a wrong answer subtracts the question value and is not marked as a pass."""
    token, headers = _start_free_choice(client, db_session, svoyak_user, "Wrong Answer Penalty")
    client.post(f"/api/play/{token}/select", json={"round_index": 0, "question_index": 2}, headers=headers)
    data = client.post(f"/api/play/{token}/answer", json={"answer": "Mutlaqo boshqa javob"}, headers=headers).json()
    assert data["points_awarded"] == -30
    assert data["total_score"] == -30
    cell = _board_cell(data["board"], 0, 2)
    assert cell["is_passed"] is False
    assert cell["points_awarded"] == -30


def test_svoyak_correct_answer_still_adds_value(client, db_session, svoyak_user):
    token, headers = _start_free_choice(client, db_session, svoyak_user, "Correct Answer Reward")
    client.post(f"/api/play/{token}/select", json={"round_index": 1, "question_index": 1}, headers=headers)
    data = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 2_20"}, headers=headers).json()
    assert data["points_awarded"] == 20
    cell = _board_cell(data["board"], 1, 1)
    assert cell["is_passed"] is False
    assert cell["points_awarded"] == 20


def test_svoyak_late_answer_scores_zero_on_board(client, db_session, svoyak_user):
    """A late answer in standard mode scores 0 (existing rule); the board reports 0 awarded, not a penalty."""
    from datetime import datetime, timezone, timedelta

    token, headers = _start_free_choice(client, db_session, svoyak_user, "Late Answer Board")
    client.post(f"/api/play/{token}/select", json={"round_index": 0, "question_index": 3}, headers=headers)
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    attempt.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=250)
    db_session.commit()

    data = client.post(f"/api/play/{token}/answer", json={"answer": "Noto'g'ri javob"}, headers=headers).json()
    assert data["timed_out"] is True
    assert data["points_awarded"] == 0
    cell = _board_cell(data["board"], 0, 3)
    assert cell["points_awarded"] == 0
    assert cell["is_passed"] is False


def test_svoyak_pass_ui_and_penalty_copy_in_template():
    """Gameplay template exposes a visible pass button, penalty explanation, and Uzbek-only mode copy."""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    assert 'id="answer-pass-btn"' in html
    assert 'onclick="handleSvoyakPass()"' in html
    assert "function handleSvoyakPass()" in html
    assert 'id="svoyak-penalty-note"' in html
    assert 'id="svoyak-scoring-rules"' in html
    assert "Noto‘g‘ri javob: savol qiymati ayiriladi" in html
    assert "O‘tkazish yoki vaqt tugashi: 0 ball" in html

    # Board cells are coloured by points actually awarded (passes never shown as a penalty)
    assert "cell.points_awarded" in html
    assert "cell.is_passed" in html

    # Mode picker no longer mixes English copy
    for english in ("<span>Free Choice Svoyak</span>", "<span>Classic Svoyak</span>",
                    "Choose any category", "Play through every category"):
        assert english not in html
    assert "<span>Erkin tanlov</span>" in html
    assert "<span>Klassik tartib</span>" in html


# =====================================================================
# SVOYAK MOBILE LAYOUT & UI CLEANUP (template-level regression guards)
# =====================================================================

def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _gameplay_section(html):
    start = html.index('id="screen-gameplay"')
    return html[start:html.index("</section>", start)]


def test_svoyak_board_mobile_layout_has_no_forced_min_width():
    """Mobile: no forced 500px grid (horizontal scroll); category stacks above its five values; desktop matrix kept."""
    html = _read("templates/index.html")
    css = _read("static/css/zakowhat-design-system.css")
    assert 'id="svoyak-grid-rows" class="game-board-rows"' in html
    assert "min-w-[500px]" not in html
    board_css = css[css.index("/* ---- Svoya Igra board"):css.index("/* ---- Break, round review and results sheets")]
    assert "min-width: 500px" not in board_css and "overflow-x" not in board_css   # nothing forces sideways scroll
    # Desktop: matrix row (theme column + five values)
    assert re.search(r"\.game-board-row \{[^}]*grid-template-columns: minmax\(0, 15rem\) minmax\(0, 1fr\);", css)
    assert re.search(r"\.game-board-cells \{[^}]*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\);", css)
    # Phone: the theme stacks above its five values
    phone = css[css.index("@media (max-width: 639px) {\n  .game-board-rows"):]
    assert re.search(r"\.game-board-row \{\s*grid-template-columns: minmax\(0, 1fr\);", phone)
    # Full theme name wraps instead of being cut off
    assert re.search(r"\.game-board-theme \{[^}]*overflow-wrap: anywhere;", css)
    assert '<div class="game-board-row">' in html and '<div class="game-board-cells">' in html


def test_svoyak_board_header_has_single_restart_and_no_duplicate_progress():
    """Board header keeps the score pill; restart stays only in the HUD (and results); progress pill removed."""
    html = _read("templates/index.html")
    gameplay = _gameplay_section(html)
    assert gameplay.count('onclick="confirmRestartGameplay()"') == 1  # HUD restart only
    assert 'id="svoyak-board-score-pill"' in gameplay
    assert "svoyak-board-progress-pill" not in html
    # Results screen restart (Restart Pack) is still present
    assert 'id="results-restart-btn"' in html


def test_gameplay_footer_label_and_classic_progress_counter():
    html = _read("templates/index.html")
    assert 'id="gameplay-format-label"' in html
    assert "isSvoyak ? 'Svoya Igra formati' : 'Zakovat 2×12 formati'" in html
    assert "formatLabel.innerText = 'Svoya Igra formati'" in html
    # Classic Svoyak shows overall position and point value
    assert "counterEl.innerText = `Savol ${overallNum} / ${overallTotal} · ${question.points || 10} ball`" in html


def test_answer_controls_mobile_row_and_desktop_inline():
    """Pass + submit share a row under the input on narrow screens; inline in the bar on desktop."""
    html = _read("templates/index.html")
    form_start = html.index('id="text-answer-form"')
    form = html[form_start:html.index("</form>", form_start)]
    css = _read("static/css/zakowhat-design-system.css")
    assert 'class="game-answer-actions"' in form
    assert 'id="answer-pass-btn"' in form  # pass sits next to submit, not in a separate row
    assert form.index('id="answer-pass-btn"') < form.index('id="answer-submit-btn"')
    # Touch target of at least 44px for both actions
    target = re.search(r"\.game-submit-btn,\s*\.game-pass-btn \{[^}]*min-height: (\d+)px;", css)
    assert target and int(target.group(1)) >= 44
    # Desktop: input and actions inline; narrow: the bar stacks and the actions share one full-width row
    assert re.search(r"\.game-answer-bar \{[^}]*display: flex;", css)
    narrow = css[css.index("@media (max-width: 720px) {\n  .game-bar {"):]
    assert re.search(r"\.game-answer-bar \{\s*flex-direction: column;", narrow)
    assert re.search(r"\.game-answer-actions \{\s*width: 100%;", narrow)
    assert "svoyak-pass-row" not in html
    # True/False quick choice form is untouched and separate
    assert 'id="true-false-actions"' in html


def test_game_mode_state_reset_between_games():
    """Starting/restoring a non-Svoyak game must not inherit Svoyak UI from a previous game in the same tab."""
    html = _read("templates/index.html")
    assert "state.gameMode = data.game_mode || null;" in html


def test_mobile_css_input_font_and_hud_wrap():
    css = _read("static/css/zakowhat-design-system.css")
    media_start = css.index("@media (max-width: 960px)")
    media_block = css[media_start:css.index("/* Responsive nav-link handled by masthead system */", media_start)]
    assert ".input-answer {\n    font-size: 16px;" in media_block
    assert ".table-hud {\n    flex-wrap: wrap;" in media_block
