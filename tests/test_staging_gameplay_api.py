import pytest
from fastapi import status
from datetime import datetime, timezone, timedelta
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User


def authenticate_editor(client, email="editor@zakowhat.uz"):
    """Registers and authenticates an internal editor user for staging tests."""
    return client.post("/api/auth/register", json={
        "display_name": "Editor",
        "email": email,
        "password": "Password123!",
    })


def create_staged_classic_pack(client, pack_num=1):
    """Creates a staged draft Classic Zakovat pack with the canonical architecture (2 rounds of 12 questions)."""
    title = f"Classic Zakovat — Pack {pack_num:02d}"
    res = client.post("/api/drafts", json={
        "title": title,
        "description": f"Official Classic Zakovat pack {pack_num:02d} for staging.",
        "game_mode": "classic_zakovat",
    })
    assert res.status_code == status.HTTP_201_CREATED
    draft_id = res.json()["version_id"]

    # 2 rounds of type zakovat_classic, 12 questions each (sequences 1..12)
    for r_seq, r_title in [(1, "1-tur"), (2, "2-tur")]:
        r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={
            "round_type": "zakovat_classic",
            "name": r_title,
        })
        assert r_res.status_code == status.HTTP_201_CREATED
        round_id = r_res.json()["round_id"]

        start_q = (r_seq - 1) * 12 + 1
        end_q = start_q + 12
        for i in range(start_q, end_q):
            q_res = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
                "text": f"Pack {pack_num:02d} Savol #{i}: Ushbu savol matni.",
                "primary_answer": f"Javob {i}",
                "accepted_answers": [f"Variant {i}"],
                "explanation": f"Izoh #{i} uchun batafsil ma'lumot.",
                "points": 1,
            })
            assert q_res.status_code == status.HTTP_201_CREATED

    val_res = client.post(f"/api/drafts/{draft_id}/validate")
    assert val_res.status_code == status.HTTP_200_OK
    assert val_res.json()["valid"] is True

    # Pack remains DRAFT (not published)
    quiz_res = client.get(f"/api/drafts/{draft_id}")
    quiz_id = quiz_res.json()["quiz_id"]
    return quiz_id, draft_id


def test_staging_quizzes_listing(client):
    """
    GET /api/play/staging/quizzes:
    Returns unreleased draft packs matching 'Classic Zakovat — Pack %'
    and strictly excludes published quizzes or unrelated drafts.
    Enforces authentication.
    """
    q1_id, _ = create_staged_classic_pack(client, pack_num=1)
    q2_id, _ = create_staged_classic_pack(client, pack_num=2)

    # Create an unrelated draft quiz (not a staged classic pack)
    client.post("/api/drafts", json={"title": "Mening Shaxsiy Kvizim", "game_mode": "modern_multiround"})

    # 1. Unauthenticated request is rejected with 401
    unauth_res = client.get("/api/play/staging/quizzes")
    assert unauth_res.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Authenticated editor can list staging packs
    authenticate_editor(client, "editor1@zakowhat.uz")
    res = client.get("/api/play/staging/quizzes")
    assert res.status_code == status.HTTP_200_OK
    staged = res.json()

    assert len(staged) == 2
    titles = [q["title"] for q in staged]
    assert "Classic Zakovat — Pack 01" in titles
    assert "Classic Zakovat — Pack 02" in titles
    assert "Mening Shaxsiy Kvizim" not in titles

    pack1 = next(q for q in staged if q["quiz_id"] == q1_id)
    assert pack1["game_mode"] == "classic_zakovat"
    assert pack1["status"] == "draft"
    assert pack1["total_questions"] == 24
    assert pack1["total_rounds"] == 2
    assert pack1["is_staging"] is True


def test_staged_packs_isolated_from_public_endpoints(client):
    """
    Public Arena endpoints must NOT expose or play staged draft packs:
    - GET /api/quizzes -> empty
    - GET /api/quizzes/{quiz_id} -> 404
    - POST /api/play/start/{quiz_id} -> 404
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=1)

    # 1. Public Arena discovery
    arena_res = client.get("/api/quizzes")
    assert arena_res.status_code == status.HTTP_200_OK
    assert len(arena_res.json()) == 0

    # 2. Public quiz detail
    detail_res = client.get(f"/api/quizzes/{quiz_id}")
    assert detail_res.status_code == status.HTTP_404_NOT_FOUND

    # 3. Public play start
    play_res = client.post(f"/api/play/start/{quiz_id}")
    assert play_res.status_code == status.HTTP_404_NOT_FOUND


def test_staging_play_start_creates_isolated_attempt(client, db_session):
    """
    POST /api/play/staging/start/{quiz_id}:
    Initializes a preview SoloAttempt with question 1 payload,
    never leaks answers, and does not consume anon cookie quota.
    Enforces authentication.
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=3)

    # 1. Unauthenticated request is rejected with 401
    unauth_res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert unauth_res.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Authenticated editor can start staging attempt
    authenticate_editor(client, "editor2@zakowhat.uz")
    start_res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    data = start_res.json()

    assert "session_token" in data
    assert data["quiz_title"] == "Classic Zakovat — Pack 03"
    assert data["game_mode"] == "classic_zakovat"
    assert data["is_staging"] is True

    # Check question state (answers must NOT leak)
    curr = data["current_state"]
    assert curr["round"]["round_type"] == "zakovat_classic"
    assert curr["round_question_index"] == 0
    assert curr["round_total_questions"] == 12
    assert "Javob" not in curr["question"]["text"]
    assert "accepted_answers" not in curr["question"]
    assert "primary_answer" not in curr["question"]

    # Verify attempt in database
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == data["session_token"]).first()
    assert attempt is not None
    assert attempt.timer_mode == "preview"
    assert attempt.anon_id is None
    assert attempt.status == "in_progress"
    assert attempt.total_score == 0
    assert attempt.total_correct == 0
    assert attempt.active_time_seconds == 0
    assert attempt.question_opened_at is not None


def test_staging_full_24_question_gameplay_loop(client, db_session):
    """
    Full real player loop for an official 24-question staged pack (2 tur x 12 savol):
    Start -> Q1-Q12 (1-tur) -> Intermediate Break -> Continue -> Q13-Q24 (2-tur) -> Round Reveal -> Idempotent Continue -> Results -> Review Modal.
    Validates scoring, timing, answer normalization, reveal state, and review data.
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=4)

    # 1. Start staging gameplay as authenticated editor
    authenticate_editor(client, "editor3@zakowhat.uz")
    start_res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]

    # 2. Answer questions 1 through 24
    # Q1-Q10: correct primary answer (1-tur)
    # Q11-Q12: correct variant answer (1-tur)
    # Q13-Q15: correct variant answer (2-tur)
    # Q16-Q20: incorrect answer (2-tur)
    # Q21-Q24: pass/blank answer (2-tur)
    for i in range(1, 25):
        if 1 <= i <= 10:
            ans = f"Javob {i}"
            expected_correct = True
            expected_points = 1
        elif 11 <= i <= 15:
            ans = f"Variant {i}"
            expected_correct = True
            expected_points = 1
        elif 16 <= i <= 20:
            ans = "Noto'g'ri javob"
            expected_correct = False
            expected_points = 0
        else:
            ans = "pass"
            expected_correct = False
            expected_points = 0

        sub_res = client.post(f"/api/play/{token}/answer", json={"answer": ans})
        assert sub_res.status_code == status.HTTP_200_OK
        sub_data = sub_res.json()

        assert sub_data["is_correct"] == expected_correct
        assert sub_data["points_awarded"] == expected_points

        if i == 12:
            # 1-tur completed -> intermediate break triggered!
            assert sub_data["round_completed"] is False
            assert sub_data["intermediate_break"] is True
            assert sub_data["quiz_completed"] is False
            assert sub_data["break_data"]["completed_questions"] == 12
            assert sub_data["break_data"]["correct_count"] == 12
            assert sub_data["break_data"]["incorrect_count"] == 0

            # Submitting during break must be blocked
            blocked = client.post(f"/api/play/{token}/answer", json={"answer": "too early"})
            assert blocked.status_code == status.HTTP_400_BAD_REQUEST

            # Explicit continuation required to start 2-tur (Q13)
            cont_break = client.post(f"/api/play/{token}/continue")
            assert cont_break.status_code == status.HTTP_200_OK
            assert cont_break.json()["status"] == "in_progress"
            assert cont_break.json()["current_state"]["round_question_index"] == 0
            assert cont_break.json()["current_state"]["round"]["sequence"] == 2
        elif i < 24:
            assert sub_data["round_completed"] is False
            assert sub_data["quiz_completed"] is False
            expected_q_idx = i if i < 12 else (i - 12)
            assert sub_data["next_state"]["round_question_index"] == expected_q_idx
        else:
            # Q24 (Q12 of 2-tur) completes the match
            assert sub_data["round_completed"] is True
            assert sub_data["quiz_completed"] is True
            assert sub_data["reveal_available"] is True

    assert sub_data["total_score"] == 15

    # 3. Round reveal (for completed 2-tur)
    rev_res = client.get(f"/api/play/{token}/reveal")
    assert rev_res.status_code == status.HTTP_200_OK
    rev_data = rev_res.json()
    assert rev_data["round_type"] == "zakovat_classic"
    assert rev_data["total_score_so_far"] == 15
    assert len(rev_data["questions"]) == 24
    assert len(rev_data["rounds"]) == 2
    assert len(rev_data["rounds"][0]["questions"]) == 12
    assert len(rev_data["rounds"][1]["questions"]) == 12

    # 4. Continue on completed attempt is idempotent
    cont_res = client.post(f"/api/play/{token}/continue")
    assert cont_res.status_code == status.HTTP_200_OK
    cont_data = cont_res.json()
    assert cont_data["status"] == "completed"
    assert cont_data["quiz_completed"] is True
    assert cont_data["total_score"] == 15
    assert cont_data["total_correct"] == 15

    # 5. Final Results
    res_res = client.get(f"/api/play/{token}/results")
    assert res_res.status_code == status.HTTP_200_OK
    res_data = res_res.json()
    assert res_data["total_score"] == 15
    assert res_data["total_correct"] == 15
    assert res_data["total_incorrect"] == 5
    assert res_data["total_unanswered"] == 4
    assert res_data["total_questions"] == 24
    assert len(res_data["rounds"]) == 2
    assert res_data["rounds"][0]["round_title"] == "1-tur"
    assert res_data["rounds"][1]["round_title"] == "2-tur"
    assert res_data["rounds"][0]["correct_count"] == 12
    assert res_data["rounds"][1]["correct_count"] == 3
    assert res_data["rounds"][1]["incorrect_count"] == 5
    assert res_data["rounds"][1]["unanswered_count"] == 4

    # 6. Answer Review Modal
    rev_modal_res = client.get(f"/api/play/{token}/review")
    assert rev_modal_res.status_code == status.HTTP_200_OK
    rev_modal_data = rev_modal_res.json()
    assert rev_modal_data["total_correct"] == 15
    assert rev_modal_data["total_incorrect"] == 5
    assert rev_modal_data["total_unanswered"] == 4
    assert len(rev_modal_data["rounds"]) == 2
    assert len(rev_modal_data["rounds"][0]["questions"]) == 12
    assert len(rev_modal_data["rounds"][1]["questions"]) == 12
    assert rev_modal_data["rounds"][0]["round_title"] == "1-tur"
    assert rev_modal_data["rounds"][1]["round_title"] == "2-tur"
    assert rev_modal_data["rounds"][1]["questions"][8]["is_unanswered"] is True

    # Verify attempt in DB is completed
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    assert attempt.status == "completed"
    assert attempt.total_correct == 15
    assert attempt.timer_mode == "preview"


def test_staging_attempts_excluded_from_leaderboard(client, db_session):
    """
    Even if completed, attempts with timer_mode='preview' are excluded from leaderboards.
    """
    quiz_id, draft_id = create_staged_classic_pack(client, pack_num=5)
    authenticate_editor(client, "editor4@zakowhat.uz")
    start_res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]

    for i in range(1, 25):
        client.post(f"/api/play/{token}/answer", json={"answer": f"Javob {i}"})
        if i == 12:
            client.post(f"/api/play/{token}/continue")
    client.post(f"/api/play/{token}/continue")

    # Leaderboard endpoint for unpublished draft returns 404
    lb_res = client.get(f"/api/quizzes/{quiz_id}/leaderboard")
    assert lb_res.status_code == status.HTTP_404_NOT_FOUND


def test_staging_html_routes(client):
    """
    GET /staging and GET /staging/{quiz_id} serve index.html successfully.
    """
    r1 = client.get("/staging")
    assert r1.status_code == status.HTTP_200_OK
    assert "text/html" in r1.headers["content-type"]
    assert "ZAKOWHAT" in r1.text

    r2 = client.get("/staging/43")
    assert r2.status_code == status.HTTP_200_OK
    assert "text/html" in r2.headers["content-type"]
    assert "ZAKOWHAT" in r2.text


def test_question_time_limit_calculation(client):
    """
    Verifies authentic Zakovat timing formula:
    - Base: 90s
    - Reading allowance: round(word_count / 2.5)
    - Bounds: [90s, 150s]
    - Blitz bounds: [20s, 45s]
    """
    from api.play import calculate_question_time_limit

    # Short question (e.g. 5 words): 90 + round(2.0) = 92s
    assert calculate_question_time_limit("Qisqa savol matni bu yerda.", "zakovat_classic") == 92

    # Medium question (25 words): 90 + 10 = 100s
    words_25 = " ".join(["so'z"] * 25)
    assert calculate_question_time_limit(words_25, "zakovat_classic") == 100

    # Long question (100 words): 90 + 40 = 130s
    words_100 = " ".join(["so'z"] * 100)
    assert calculate_question_time_limit(words_100, "zakovat_classic") == 130

    # Very long question (200 words): capped at 150s
    words_200 = " ".join(["so'z"] * 200)
    assert calculate_question_time_limit(words_200, "zakovat_classic") == 150

    # Minimum bound enforced: empty or 0 words -> 90s
    assert calculate_question_time_limit("", "zakovat_classic") == 90

    # Blitz mode bounds [20, 45]
    assert calculate_question_time_limit("", "blitz") == 20
    assert calculate_question_time_limit("Bir ikki uch", "blitz") == 21
    assert calculate_question_time_limit(words_200, "blitz") == 45


def test_time_limit_seconds_present_in_question_response(client):
    """
    Every question response payload must expose time_limit_seconds.
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=6)
    authenticate_editor(client, "editor5@zakowhat.uz")
    res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert res.status_code == status.HTTP_201_CREATED
    curr = res.json()["current_state"]
    assert "time_limit_seconds" in curr["question"]
    assert curr["question"]["time_limit_seconds"] >= 60


def test_active_time_capped_by_question_time_limit(client, db_session):
    """
    Active time tracking in submit_answer must be strictly capped by question time limit,
    preventing inactive browser tabs from artificially bloating gameplay time.
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=7)
    authenticate_editor(client, "editor6@zakowhat.uz")
    start_res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    # Simulate user opened question 10 minutes (600s) ago
    attempt.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    db_session.commit()

    sub_res = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1"})
    assert sub_res.status_code == status.HTTP_200_OK

    db_session.refresh(attempt)
    # Active time must NOT be 600s; must be capped by time limit (e.g. 60-120s)
    assert attempt.active_time_seconds <= 120
    assert attempt.active_time_seconds > 0


def test_staging_timer_modes_selection(client, db_session):
    """
    Tests pre-game timer settings:
    - 'no_timer': time_limit_seconds is 0, active time is not capped
    - 'with_timer': time_limit_seconds is >= 60
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=9)
    authenticate_editor(client, "editor7@zakowhat.uz")

    # 1. Start with no_timer
    r_no_timer = client.post(f"/api/play/staging/start/{quiz_id}", json={"timer_mode": "no_timer"})
    assert r_no_timer.status_code == status.HTTP_201_CREATED
    data_no_timer = r_no_timer.json()
    assert data_no_timer["current_state"]["question"]["time_limit_seconds"] == 0
    token_no_timer = data_no_timer["session_token"]

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token_no_timer).first()
    assert (attempt.attempt_metadata or {}).get("timer_mode") == "no_timer"

    # Simulate 150 seconds pass
    attempt.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=150)
    db_session.commit()
    sub = client.post(f"/api/play/{token_no_timer}/answer", json={"answer": "Javob 1"})
    assert sub.status_code == status.HTTP_200_OK
    db_session.refresh(attempt)
    # In no_timer mode, timing is NOT capped to 120s
    assert attempt.active_time_seconds >= 140

    # 2. Start with with_timer
    r_with_timer = client.post(f"/api/play/staging/start/{quiz_id}", json={"timer_mode": "with_timer"})
    assert r_with_timer.status_code == status.HTTP_201_CREATED
    data_with_timer = r_with_timer.json()
    assert data_with_timer["current_state"]["question"]["time_limit_seconds"] >= 60


def test_anonymous_user_staging_does_not_consume_quota(client, db_session):
    """
    Anonymous visitor cannot access staging (401),
    and when authenticated as an editor, staging play does not consume anon quota.
    """
    quiz_id, _ = create_staged_classic_pack(client, pack_num=8)

    # 1. Anonymous visitor is rejected
    res_anon = client.post(f"/api/play/staging/start/{quiz_id}")
    assert res_anon.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Authenticated editor starts staging attempt
    authenticate_editor(client, "editor8@zakowhat.uz")
    res = client.post(f"/api/play/staging/start/{quiz_id}")
    assert res.status_code == status.HTTP_201_CREATED
    token = res.json()["session_token"]

    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == token).first()
    assert attempt.anon_id is None
    assert attempt.timer_mode == "preview"
