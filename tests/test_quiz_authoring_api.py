import pytest
from fastapi import status
from models import Question, AcceptedAnswer, Quiz, QuizVersion, Round, RoundQuestion
from api.quizzes import compile_published_manifest


def test_create_draft_and_load_empty(client, db_session):
    """Verify creating an empty draft initializes Quiz + QuizVersion (status='draft', published_manifest=NULL)."""
    res = client.post("/api/drafts", json={
        "title": "Championship 2026",
        "description": "Bahorgi mavsum",
        "game_mode": "modern_multiround"
    })
    assert res.status_code == status.HTTP_201_CREATED
    data = res.json()
    assert data["success"] is True
    draft_id = data["version_id"]

    # Load draft
    load_res = client.get(f"/api/drafts/{draft_id}")
    assert load_res.status_code == status.HTTP_200_OK
    draft_data = load_res.json()
    assert draft_data["title"] == "Championship 2026"
    assert draft_data["description"] == "Bahorgi mavsum"
    assert draft_data["game_mode"] == "modern_multiround"
    assert draft_data["status"] == "draft"
    assert draft_data["rounds_count"] == 0
    assert draft_data["total_questions"] == 0

    # DB Level checks
    v = db_session.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    assert v is not None
    assert v.status == "draft"
    assert v.published_manifest is None


def test_add_and_reorder_rounds(client, db_session):
    """Verify adding canonical rounds and reordering them."""
    # 1. Create draft
    res = client.post("/api/drafts", json={"title": "Multi-Round Draft"})
    draft_id = res.json()["version_id"]

    # 2. Add 3 rounds
    r1 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "gulmisiz_rayhonmisiz"}).json()
    r2 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zanjir"}).json()
    r3 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "aldama_meni"}).json()

    assert r1["sequence"] == 1 and r1["round_type"] == "gulmisiz_rayhonmisiz"
    assert r2["sequence"] == 2 and r2["round_type"] == "zanjir"
    assert r3["sequence"] == 3 and r3["round_type"] == "aldama_meni"

    # 3. Reorder rounds: r2, r3, r1
    reorder_res = client.put(f"/api/drafts/{draft_id}/rounds/reorder", json={
        "round_ids": [r2["round_id"], r3["round_id"], r1["round_id"]]
    })
    assert reorder_res.status_code == status.HTTP_200_OK

    # 4. Verify loaded order
    loaded = client.get(f"/api/drafts/{draft_id}").json()
    assert len(loaded["rounds"]) == 3
    assert loaded["rounds"][0]["round_id"] == r2["round_id"]
    assert loaded["rounds"][0]["sequence"] == 1
    assert loaded["rounds"][1]["round_id"] == r3["round_id"]
    assert loaded["rounds"][1]["sequence"] == 2
    assert loaded["rounds"][2]["round_id"] == r1["round_id"]
    assert loaded["rounds"][2]["sequence"] == 3


def test_author_question_gulmisiz_mcq(client, db_session):
    """Verify authoring a MCQ question for Gulmisiz, rayhonmisiz? with 4 options and 1 correct key."""
    # Draft + Round
    draft = client.post("/api/drafts", json={"title": "MCQ Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "gulmisiz_rayhonmisiz"}).json()

    payload = {
        "text": "O'zbekistonning poytaxti qaysi shahar?",
        "points": 5,
        "options": [
            {"key": "A", "text": "Samarqand"},
            {"key": "B", "text": "Toshkent"},
            {"key": "C", "text": "Buxoro"},
            {"key": "D", "text": "Xiva"}
        ],
        "correct_option": "B"
    }
    q_res = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json=payload)
    assert q_res.status_code == status.HTTP_201_CREATED
    q_data = q_res.json()
    assert q_data["primary_answer"] == "B"
    assert q_data["question_type"] == "mcq"
    assert q_data["points"] == 5

    # DB checks
    q_db = db_session.query(Question).filter(Question.id == q_data["question_id"]).first()
    assert q_db is not None
    assert q_db.round_id is None, "CRITICAL: Question.round_id must remain NULL!"
    assert len(q_db.options) == 4
    assert q_db.options[1]["key"] == "B"
    assert q_db.options[1]["text"] == "Toshkent"

    # Accepted answers: primary 'B', alternative 'Toshkent'
    answers = q_db.accepted_answers
    assert len(answers) == 2
    assert any(a.is_primary and a.answer_text == "B" for a in answers)
    assert any(not a.is_primary and a.answer_text == "Toshkent" for a in answers)


def test_author_question_gulmisiz_invalid_options(client):
    """Verify rejection if MCQ does not have exactly 4 options or has an invalid correct key."""
    draft = client.post("/api/drafts", json={"title": "Invalid MCQ Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "gulmisiz_rayhonmisiz"}).json()

    # Only 3 options
    res1 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Savol",
        "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}, {"key": "C", "text": "3"}],
        "correct_option": "A"
    })
    assert res1.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # Invalid correct option
    res2 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Savol",
        "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}, {"key": "C", "text": "3"}, {"key": "D", "text": "4"}],
        "correct_option": "E"
    })
    assert res2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_author_question_zanjir(client, db_session):
    """Verify authoring a chain question with primary and alternative answers."""
    draft = client.post("/api/drafts", json={"title": "Zanjir Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "zanjir"}).json()

    res = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Ertak qahramoni bo'lgan bahodir yigit?",
        "primary_answer": "Rustam",
        "accepted_answers": ["Rustami doston"],
        "points": 1
    })
    assert res.status_code == status.HTTP_201_CREATED
    q_data = res.json()
    assert q_data["primary_answer"] == "Rustam"

    q_db = db_session.query(Question).filter(Question.id == q_data["question_id"]).first()
    assert q_db.round_id is None
    assert len(q_db.accepted_answers) == 2


def test_author_question_aldama_meni_boolean(client, db_session):
    """Verify authoring a True/False statement with boolean normalization and synonyms."""
    draft = client.post("/api/drafts", json={"title": "Aldama Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "aldama_meni"}).json()

    # Fact that is true
    res_true = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Yer Quyosh atrofida aylanadi.",
        "correct_boolean": True,
        "explanation": "Bu fanga ma'lum astronomik haqiqat."
    })
    assert res_true.status_code == status.HTTP_201_CREATED
    q_data = res_true.json()
    assert q_data["primary_answer"] == "ha"
    assert "rost" in q_data["accepted_answers"]

    # Fact that is false
    res_false = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Oy Quyoshdan kattaroq.",
        "correct_boolean": False
    })
    assert res_false.status_code == status.HTTP_201_CREATED
    assert res_false.json()["primary_answer"] == "yo'q"
    assert "yolg'on" in res_false.json()["accepted_answers"]


def test_author_question_rasmiyatchilik_media(client, db_session):
    """Verify media question authoring with image URL."""
    draft = client.post("/api/drafts", json={"title": "Media Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "rasmiyatchilik"}).json()

    res = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Rasmdagi bino qaysi shaharda joylashgan?",
        "media_url": "https://images.unsplash.com/photo-1541447271487-09612b3f49f7",
        "primary_answer": "Parij",
        "accepted_answers": ["Paris"]
    })
    assert res.status_code == status.HTTP_201_CREATED
    q_db = db_session.query(Question).filter(Question.id == res.json()["question_id"]).first()
    assert q_db.media_url == "https://images.unsplash.com/photo-1541447271487-09612b3f49f7"
    assert q_db.question_type == "media"


def test_author_question_mantiqqasqon_logic(client, db_session):
    """Verify logic/connection question authoring with explanation."""
    draft = client.post("/api/drafts", json={"title": "Mantiqqasqon Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "mantiqqasqon"}).json()

    res = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Ushbu to'rtta so'zni nima birlashtiradi: Qizil, Sariq, Yashil, Ko'k?",
        "primary_answer": "Kamalak",
        "explanation": "Ular barchasi kamalak ranglaridir."
    })
    assert res.status_code == status.HTTP_201_CREATED
    q_db = db_session.query(Question).filter(Question.id == res.json()["question_id"]).first()
    assert q_db.explanation == "Ular barchasi kamalak ranglaridir."


def test_author_question_vabank_value(client, db_session):
    """Verify vabank question with configurable points."""
    draft = client.post("/api/drafts", json={"title": "Vabank Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "vabank"}).json()

    res = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Eng qiyin savol",
        "primary_answer": "Javob",
        "points": 50
    })
    assert res.status_code == status.HTTP_201_CREATED
    q_db = db_session.query(Question).filter(Question.id == res.json()["question_id"]).first()
    assert q_db.points == 50
    assert q_db.round_id is None


def test_edit_authored_question(client, db_session):
    """Verify in-place editing of an authored question in a draft round."""
    draft = client.post("/api/drafts", json={"title": "Edit Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "zanjir"}).json()

    q_create = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Eski matn",
        "primary_answer": "Eski javob",
        "points": 1
    }).json()
    rq_id = q_create["round_question_id"]

    # Update question
    edit_res = client.put(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions/{rq_id}", json={
        "text": "Yangi yangilangan matn",
        "primary_answer": "Yangi javob",
        "points": 10,
        "explanation": "Yangi izoh"
    })
    assert edit_res.status_code == status.HTTP_200_OK

    # Verify DB
    q_db = db_session.query(Question).filter(Question.id == q_create["question_id"]).first()
    assert q_db.text == "Yangi yangilangan matn"
    assert q_db.points == 10
    assert q_db.explanation == "Yangi izoh"
    prim = next(a for a in q_db.accepted_answers if a.is_primary)
    assert prim.answer_text == "Yangi javob"


def test_reorder_questions_within_round(client, db_session):
    """Verify reordering questions within a round updates sequences properly."""
    draft = client.post("/api/drafts", json={"title": "Reorder Questions Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "zanjir"}).json()

    q1 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={"text": "Q1", "primary_answer": "A1"}).json()
    q2 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={"text": "Q2", "primary_answer": "A2"}).json()
    q3 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={"text": "Q3", "primary_answer": "A3"}).json()

    # Reorder to Q3, Q1, Q2
    res = client.put(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions/reorder", json={
        "round_question_ids": [q3["round_question_id"], q1["round_question_id"], q2["round_question_id"]]
    })
    assert res.status_code == status.HTTP_200_OK

    # Verify loaded order
    loaded = client.get(f"/api/drafts/{draft['version_id']}").json()
    questions = loaded["rounds"][0]["questions"]
    assert len(questions) == 3
    assert questions[0]["round_question_id"] == q3["round_question_id"]
    assert questions[0]["sequence"] == 1
    assert questions[1]["round_question_id"] == q1["round_question_id"]
    assert questions[1]["sequence"] == 2
    assert questions[2]["round_question_id"] == q2["round_question_id"]
    assert questions[2]["sequence"] == 3


def test_delete_question_from_round_does_not_destroy_question(client, db_session):
    """
    CRITICAL (Correction #1):
    Removing a question from a draft removes the RoundQuestion association.
    It MUST NOT automatically delete the global reusable Question record.
    """
    draft = client.post("/api/drafts", json={"title": "Delete Q Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "zanjir"}).json()

    q1 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={"text": "Keep Me", "primary_answer": "K1"}).json()
    q2 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={"text": "Unlink Me", "primary_answer": "U1"}).json()

    # Delete q2 from round
    del_res = client.delete(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions/{q2['round_question_id']}")
    assert del_res.status_code == status.HTTP_200_OK

    # RoundQuestion for q2 must be gone
    rq_db = db_session.query(RoundQuestion).filter(RoundQuestion.id == q2["round_question_id"]).first()
    assert rq_db is None

    # Question record itself MUST STILL EXIST!
    q2_db = db_session.query(Question).filter(Question.id == q2["question_id"]).first()
    assert q2_db is not None, "Question record must NOT be destroyed upon unlink from draft!"

    # Remaining question is re-sequenced
    rq1_db = db_session.query(RoundQuestion).filter(RoundQuestion.id == q1["round_question_id"]).first()
    assert rq1_db.sequence == 1


def test_delete_round(client, db_session):
    """Verify deleting a round cascades RoundQuestion links without hard-deleting Questions."""
    draft = client.post("/api/drafts", json={"title": "Delete Round Draft"}).json()
    r1 = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "zanjir"}).json()
    r2 = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "aldama_meni"}).json()

    q1 = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r1['round_id']}/questions", json={"text": "Q1", "primary_answer": "A1"}).json()

    # Delete r1
    del_res = client.delete(f"/api/drafts/{draft['version_id']}/rounds/{r1['round_id']}")
    assert del_res.status_code == status.HTTP_200_OK

    # r1 is gone, r2 re-sequenced to 1
    loaded = client.get(f"/api/drafts/{draft['version_id']}").json()
    assert len(loaded["rounds"]) == 1
    assert loaded["rounds"][0]["round_id"] == r2["round_id"]
    assert loaded["rounds"][0]["sequence"] == 1

    # Question q1 still exists
    assert db_session.query(Question).filter(Question.id == q1["question_id"]).first() is not None


def test_reopen_and_update_draft(client, db_session):
    """Verify updating title, description, game mode, and reloading."""
    draft = client.post("/api/drafts", json={"title": "Original Title"}).json()
    draft_id = draft["version_id"]

    put_res = client.put(f"/api/drafts/{draft_id}", json={
        "title": "Renamed Championship Draft",
        "description": "Updated Description",
        "game_mode": "classic_zakovat"
    })
    assert put_res.status_code == status.HTTP_200_OK
    assert put_res.json()["title"] == "Renamed Championship Draft"

    loaded = client.get(f"/api/drafts/{draft_id}").json()
    assert loaded["title"] == "Renamed Championship Draft"
    assert loaded["description"] == "Updated Description"
    assert loaded["game_mode"] == "classic_zakovat"


def test_draft_validation_endpoint(client):
    """Verify /api/drafts/{id}/validate reports errors on empty rounds and passes on complete drafts."""
    draft = client.post("/api/drafts", json={"title": "Validation Draft"}).json()
    draft_id = draft["version_id"]

    # 1. Empty draft -> errors
    val1 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val1["valid"] is False
    assert any("kamida bitta raund" in e.lower() for e in val1["errors"])

    # 2. Add round with no questions -> errors
    r1 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "gulmisiz_rayhonmisiz"}).json()
    val2 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val2["valid"] is False
    assert any("hech qanday savol yo'q" in e.lower() for e in val2["errors"])

    # 3. Add valid question -> valid is True
    client.post(f"/api/drafts/{draft_id}/rounds/{r1['round_id']}/questions", json={
        "text": "Savol",
        "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}, {"key": "C", "text": "3"}, {"key": "D", "text": "4"}],
        "correct_option": "A"
    })
    val3 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val3["valid"] is True
    assert len(val3["errors"]) == 0


def test_mantiqqasqon_hidden_rule_not_leaked_before_reveal(client, db_session):
    """
    CRITICAL (Correction #3):
    The mantiqqasqon explanation / hidden rule must NOT be exposed to players
    through discovery or gameplay states before reveal.
    """
    # Create draft
    draft = client.post("/api/drafts", json={"title": "Mantiqqasqon Security Quiz"}).json()
    draft_id = draft["version_id"]
    quiz_id = draft["quiz_id"]

    # Add round with hidden rule in config
    r = client.post(f"/api/drafts/{draft_id}/rounds", json={
        "round_type": "mantiqqasqon",
        "config": {"theme": "Kosmos", "hidden_rule": "SUPER_SECRET_RULE_DO_NOT_LEAK"}
    }).json()

    # Add question with secret explanation
    q = client.post(f"/api/drafts/{draft_id}/rounds/{r['round_id']}/questions", json={
        "text": "Savol matni",
        "primary_answer": "Javob",
        "explanation": "SECRET_EXPLANATION_FOR_REVEAL_ONLY"
    }).json()

    # Simulate publishing this version for gameplay test
    version = db_session.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    version.published_manifest = compile_published_manifest(version, db_session)
    version.status = "published"
    db_session.commit()

    # 1. Check discovery endpoint: GET /api/quizzes/{quiz_id}
    detail_res = client.get(f"/api/quizzes/{quiz_id}")
    assert detail_res.status_code == status.HTTP_200_OK
    detail_text = detail_res.text
    assert "SUPER_SECRET_RULE_DO_NOT_LEAK" not in detail_text, "Hidden rule leaked in /api/quizzes/{id}!"
    assert "SECRET_EXPLANATION_FOR_REVEAL_ONLY" not in detail_text, "Explanation leaked in /api/quizzes/{id}!"

    # 2. Check gameplay start endpoint: POST /api/play/start/{quiz_id}
    play_res = client.post(f"/api/play/start/{quiz_id}")
    assert play_res.status_code == status.HTTP_201_CREATED
    play_text = play_res.text
    assert "SUPER_SECRET_RULE_DO_NOT_LEAK" not in play_text, "Hidden rule leaked in /api/play/start!"
    assert "SECRET_EXPLANATION_FOR_REVEAL_ONLY" not in play_text, "Explanation leaked in /api/play/start!"


# =========================================================
# REGRESSION TESTS: QUESTION BANK ISOLATION & LIFECYCLE
# =========================================================

def test_authored_questions_never_appear_in_question_bank(client, db_session, monkeypatch):
    """
    Verify Question Bank Isolation:
    Questions authored inside a draft have status='draft' and MUST NEVER appear
    in Question Bank endpoints (/api/questions, /api/questions/filters, /api/questions/{id}).
    """
    import uuid
    owner_email = f"owner_authoring_test_{uuid.uuid4().hex[:8]}@example.test"
    monkeypatch.setenv("OWNER_EMAIL", owner_email)

    # 1. Create a draft and author a question
    draft = client.post("/api/drafts", json={"title": "Isolated Draft"}).json()
    r = client.post(f"/api/drafts/{draft['version_id']}/rounds", json={"round_type": "zanjir"}).json()

    create_res = client.post(f"/api/drafts/{draft['version_id']}/rounds/{r['round_id']}/questions", json={
        "text": "Maxfiy Qoralama Savoli #12345",
        "primary_answer": "Sir",
    })
    assert create_res.status_code == status.HTTP_201_CREATED
    q_data = create_res.json()
    q_id = q_data["question_id"]

    # 2. Check Question in DB has status='draft' and round_id=None
    q_db = db_session.query(Question).filter(Question.id == q_id).first()
    assert q_db.status == "draft"
    assert q_db.round_id is None

    # Authenticate as owner to query internal Question Bank
    client.post("/api/auth/register", json={
        "email": owner_email,
        "password": "ValidPassword123!",
        "display_name": "Owner Authoring Tester",
    })

    # 3. Query Question Bank list endpoint (/api/questions)
    qb_res = client.get("/api/questions")
    assert qb_res.status_code == status.HTTP_200_OK
    all_texts = [item["text"] for item in qb_res.json()["items"]]
    assert "Maxfiy Qoralama Savoli #12345" not in all_texts

    # 4. Query Question Bank with status=ready
    qb_ready = client.get("/api/questions?status=ready")
    assert qb_ready.status_code == status.HTTP_200_OK
    assert "Maxfiy Qoralama Savoli #12345" not in [item["text"] for item in qb_ready.json()["items"]]

    # 5. Query Question Bank with status=draft (must return empty)
    qb_draft = client.get("/api/questions?status=draft")
    assert qb_draft.status_code == status.HTTP_200_OK
    assert qb_draft.json()["total"] == 0
    assert len(qb_draft.json()["items"]) == 0

    # 6. Search Question Bank by exact text (must return empty)
    qb_search = client.get("/api/questions?search=12345")
    assert qb_search.status_code == status.HTTP_200_OK
    assert qb_search.json()["total"] == 0

    # 7. Access Question Bank detail endpoint for draft question (must 404)
    qb_detail = client.get(f"/api/questions/{q_id}")
    assert qb_detail.status_code == status.HTTP_404_NOT_FOUND

    # 8. Check Question Bank filter options (status 'draft' must NOT appear)
    qb_filters = client.get("/api/questions/filters").json()
    assert "draft" not in qb_filters["statuses"]


def test_delete_draft_cleans_up_exclusive_authored_questions(client, db_session):
    """
    Verify Draft Lifecycle & Orphan Prevention:
    Deleting a draft cleans up questions authored exclusively inside it,
    preventing dangling or orphaned rows in the questions table.
    """
    # 1. Create a draft and author 2 questions
    draft = client.post("/api/drafts", json={"title": "Draft to Delete"}).json()
    draft_id = draft["version_id"]
    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zanjir"}).json()

    q1 = client.post(f"/api/drafts/{draft_id}/rounds/{r['round_id']}/questions", json={
        "text": "Savol Bir",
        "primary_answer": "Javob 1",
    }).json()
    q2 = client.post(f"/api/drafts/{draft_id}/rounds/{r['round_id']}/questions", json={
        "text": "Savol Ikki",
        "primary_answer": "Javob 2",
    }).json()

    q1_id = q1["question_id"]
    q2_id = q2["question_id"]

    # Verify both exist in DB with status='draft'
    assert db_session.query(Question).filter(Question.id == q1_id).first() is not None
    assert db_session.query(Question).filter(Question.id == q2_id).first() is not None

    # 2. Delete the draft
    del_res = client.delete(f"/api/drafts/{draft_id}")
    assert del_res.status_code == status.HTTP_200_OK

    # 3. Verify QuizVersion, Round, and RoundQuestion are deleted
    assert db_session.query(QuizVersion).filter(QuizVersion.id == draft_id).first() is None
    assert db_session.query(Round).filter(Round.id == r["round_id"]).first() is None

    # 4. Verify exclusive draft questions are cleanly deleted (no orphaned rows)
    assert db_session.query(Question).filter(Question.id == q1_id).first() is None
    assert db_session.query(Question).filter(Question.id == q2_id).first() is None
    assert db_session.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q1_id).count() == 0
    assert db_session.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q2_id).count() == 0


def test_delete_draft_preserves_question_bank_items(client, db_session):
    """
    Verify Draft Lifecycle Safety:
    Deleting a draft does NOT delete reusable Question Bank questions linked via RoundQuestion.
    """
    # 1. Seed a Question Bank item
    qb_q = Question(
        text="Question Bank Savoli",
        status="ready",
        category="Tarix",
        points=1,
        source_meta={"source_name": "Editorial Pack"}
    )
    db_session.add(qb_q)
    db_session.commit()
    db_session.refresh(qb_q)
    qb_id = qb_q.id

    # 2. Create draft and attach Question Bank item via RoundQuestion
    draft = client.post("/api/drafts", json={"title": "Draft with QB Item"}).json()
    draft_id = draft["version_id"]
    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "standard"}).json()

    rq = RoundQuestion(round_id=r["round_id"], question_id=qb_id, sequence=1)
    db_session.add(rq)
    db_session.commit()

    # 3. Delete the draft
    del_res = client.delete(f"/api/drafts/{draft_id}")
    assert del_res.status_code == status.HTTP_200_OK

    # 4. Verify Question Bank item is still intact and ready
    surviving_q = db_session.query(Question).filter(Question.id == qb_id).first()
    assert surviving_q is not None
    assert surviving_q.status == "ready"


def test_classic_zakovat_12_plus_12_structure_and_validation(client, db_session):
    """
    Verify Classic Zakovat 12+12 Structure:
    - 2 rounds of 12 questions each (total 24 questions).
    - Question sequences 1..12 in Round 1, and 1..12 in Round 2.
    - Preserved without sequence conflicts or constraints.
    - Validation passes for complete 12+12 draft, warns for incomplete structure.
    """
    # 1. Create Classic Zakovat draft
    draft = client.post("/api/drafts", json={
        "title": "Klassik Zakovat Chempionati",
        "game_mode": "classic_zakovat"
    }).json()
    draft_id = draft["version_id"]

    # 2. Add Round 1
    r1 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zakovat_classic"}).json()
    r1_id = r1["round_id"]

    # Partial validation: 1 round, 0 questions -> should have warnings/errors
    val_partial = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val_partial["valid"] is False  # empty round is an error

    # Author 12 questions in Round 1
    for i in range(1, 13):
        q_res = client.post(f"/api/drafts/{draft_id}/rounds/{r1_id}/questions", json={
            "text": f"1-tur Savoli #{i}",
            "primary_answer": f"Javob {i}",
            "points": 1
        })
        assert q_res.status_code == status.HTTP_201_CREATED
        assert q_res.json()["sequence"] == i

    # 3. Add Round 2
    r2 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zakovat_classic"}).json()
    r2_id = r2["round_id"]

    # Author 12 questions in Round 2
    for i in range(1, 13):
        q_res = client.post(f"/api/drafts/{draft_id}/rounds/{r2_id}/questions", json={
            "text": f"2-tur Savoli #{i}",
            "primary_answer": f"Javob {i}",
            "points": 1
        })
        assert q_res.status_code == status.HTTP_201_CREATED
        assert q_res.json()["sequence"] == i

    # 4. Load full draft structure
    loaded = client.get(f"/api/drafts/{draft_id}").json()
    assert loaded["game_mode"] == "classic_zakovat"
    assert loaded["rounds_count"] == 2
    assert loaded["total_questions"] == 24

    round1_qs = loaded["rounds"][0]["questions"]
    round2_qs = loaded["rounds"][1]["questions"]
    assert len(round1_qs) == 12
    assert len(round2_qs) == 12

    # Check sequences 1..12 in each
    assert [q["sequence"] for q in round1_qs] == list(range(1, 13))
    assert [q["sequence"] for q in round2_qs] == list(range(1, 13))

    # 5. Full validation
    val_full = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val_full["valid"] is True
    assert len(val_full["errors"]) == 0
    # No 12+12 warnings because structure is exactly 2 rounds x 12 questions
    zakovat_warnings = [w for w in val_full["warnings"] if "Zakovat" in w or "turda" in w]
    assert len(zakovat_warnings) == 0


def test_svoyak_points_authoring_and_negative_scoring(client, db_session):
    """
    Verify Svoyak Authoring and Scoring Engine:
    - Authoring questions with 10, 20, 30, 40, 50 points.
    - Svoяk scoring: Correct adds points (+val), Wrong deducts points (-val), Pass gives 0.
    """
    # 1. Create Svoyak draft
    draft = client.post("/api/drafts", json={
        "title": "Svoяk Kubogi",
        "game_mode": "svoyak"
    }).json()
    draft_id = draft["version_id"]
    quiz_id = draft["quiz_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "svoyak_theme"}).json()
    round_id = r["round_id"]

    # 2. Author 5 questions with points 10, 20, 30, 40, 50
    points_list = [10, 20, 30, 40, 50]
    for pts in points_list:
        res = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
            "text": f"Svoyak savol {pts} ball",
            "primary_answer": f"Javob {pts}",
            "points": pts
        })
        assert res.status_code == status.HTTP_201_CREATED
        assert res.json()["points"] == pts

    # Verify loaded draft structure
    loaded = client.get(f"/api/drafts/{draft_id}").json()
    theme_qs = loaded["rounds"][0]["questions"]
    assert [q["points"] for q in theme_qs] == [10, 20, 30, 40, 50]

    # 3. Simulate publishing for gameplay session
    version = db_session.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    version.published_manifest = compile_published_manifest(version, db_session)
    version.status = "published"
    db_session.commit()

    # 4. Start gameplay attempt
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]

    # Question 1 (10 pts): Correct answer -> score = 10
    ans1 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 10"})
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["is_correct"] is True
    assert ans1.json()["points_awarded"] == 10
    client.post(f"/api/play/{token}/reveal")
    client.post(f"/api/play/{token}/continue")

    # Question 2 (20 pts): Incorrect answer -> score = 10 - 20 = -10 (Negative Scoring!)
    ans2 = client.post(f"/api/play/{token}/answer", json={"answer": "Noto'g'ri javob"})
    assert ans2.status_code == status.HTTP_200_OK
    assert ans2.json()["is_correct"] is False
    assert ans2.json()["points_awarded"] == -20
    client.post(f"/api/play/{token}/reveal")
    client.post(f"/api/play/{token}/continue")

    # Question 3 (30 pts): Blank / Pass -> points_awarded = 0
    ans3 = client.post(f"/api/play/{token}/answer", json={"answer": "pass"})
    assert ans3.status_code == status.HTTP_200_OK
    assert ans3.json()["points_awarded"] == 0
    client.post(f"/api/play/{token}/reveal")
    client.post(f"/api/play/{token}/continue")

    # Question 4 (40 pts): Correct answer -> score = -10 + 40 = 30
    ans4 = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 40"})
    assert ans4.status_code == status.HTTP_200_OK
    assert ans4.json()["is_correct"] is True
    assert ans4.json()["points_awarded"] == 40
    client.post(f"/api/play/{token}/reveal")
    client.post(f"/api/play/{token}/continue")

    # Question 5 (50 pts): Incorrect answer -> score = 30 - 50 = -20
    ans5 = client.post(f"/api/play/{token}/answer", json={"answer": "Xato"})
    assert ans5.status_code == status.HTTP_200_OK
    assert ans5.json()["is_correct"] is False
    assert ans5.json()["points_awarded"] == -50
    client.post(f"/api/play/{token}/reveal")
    client.post(f"/api/play/{token}/continue")

    # Check final results
    results_res = client.get(f"/api/play/{token}/results")
    assert results_res.status_code == status.HTTP_200_OK
    assert results_res.json()["total_score"] == -20
