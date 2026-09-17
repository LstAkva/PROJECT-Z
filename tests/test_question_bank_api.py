import pytest
from fastapi import status
from models import Question, AcceptedAnswer, Quiz, QuizVersion, Round, RoundQuestion
from api.quizzes import CANONICAL_ROUND_TYPES


@pytest.fixture
def sample_question_bank(db_session):
    """Creates a diverse sample set of questions in the Question Bank."""
    q1 = Question(
        text="Alisher Navoiy qaysi asarni yozgan?",
        status="ready",
        category="Adabiyot",
        points=10,
        default_points=10,
        source_meta={"source_name": "Test Source Alpha", "round_type": "standard"}
    )
    q2 = Question(
        text="Amir Temur nechanchi yilda tug'ilgan?",
        status="ready",
        category="Tarix",
        points=20,
        default_points=20,
        source_meta={"source_name": "Test Source Beta", "round_type": "blitz"}
    )
    q3 = Question(
        text="O'zbekiston poytaxti qaysi shahar?",
        status="needs_review",
        category="Geografiya",
        points=10,
        default_points=10,
        source_meta={"source_name": "Test Source Alpha", "round_type": "standard"}
    )
    db_session.add_all([q1, q2, q3])
    db_session.flush()

    ans1_prim = AcceptedAnswer(question_id=q1.id, answer_text="Xamsa", is_primary=True)
    ans1_alt = AcceptedAnswer(question_id=q1.id, answer_text="Xamsa dostoni", is_primary=False)
    ans2_prim = AcceptedAnswer(question_id=q2.id, answer_text="1336", is_primary=True)
    ans3_prim = AcceptedAnswer(question_id=q3.id, answer_text="Toshkent", is_primary=True)
    db_session.add_all([ans1_prim, ans1_alt, ans2_prim, ans3_prim])
    db_session.commit()

    return [q1, q2, q3]


def test_list_questions_pagination(client, sample_question_bank):
    """Verify paginated listing of Question Bank questions."""
    res = client.get("/api/questions?page=1&page_size=2")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total_pages"] == 2
    assert len(data["items"]) == 2


def test_list_questions_search(client, sample_question_bank):
    """Verify search filter by substring on question text."""
    res = client.get("/api/questions?search=Navoiy")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["text"] == "Alisher Navoiy qaysi asarni yozgan?"
    assert data["items"][0]["primary_answer"] == "Xamsa"


def test_list_questions_filter_status(client, sample_question_bank):
    """Verify filtering by status (ready vs needs_review)."""
    res_ready = client.get("/api/questions?status=ready")
    assert res_ready.status_code == status.HTTP_200_OK
    assert res_ready.json()["total"] == 2
    for item in res_ready.json()["items"]:
        assert item["status"] == "ready"

    res_review = client.get("/api/questions?status=needs_review")
    assert res_review.status_code == status.HTTP_200_OK
    assert res_review.json()["total"] == 1
    assert res_review.json()["items"][0]["status"] == "needs_review"


def test_list_questions_filter_category(client, sample_question_bank):
    """Verify filtering by category."""
    res = client.get("/api/questions?category=Tarix")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["category"] == "Tarix"


def test_get_filter_options(client, sample_question_bank):
    """Verify /api/questions/filters returns available categories, statuses, sources."""
    res = client.get("/api/questions/filters")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert "ready" in data["statuses"]
    assert "needs_review" in data["statuses"]
    assert "Adabiyot" in data["categories"]
    assert "Tarix" in data["categories"]


def test_get_question_detail_success(client, sample_question_bank):
    """Verify full question detail including accepted answers."""
    q1 = sample_question_bank[0]
    res = client.get(f"/api/questions/{q1.id}")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["id"] == q1.id
    assert data["text"] == "Alisher Navoiy qaysi asarni yozgan?"
    assert data["primary_answer"] == "Xamsa"
    assert len(data["accepted_answers"]) == 2
    assert any(a["answer_text"] == "Xamsa dostoni" and not a["is_primary"] for a in data["accepted_answers"])


def test_get_question_detail_not_found(client):
    """Verify 404 for missing question ID."""
    res = client.get("/api/questions/999999")
    assert res.status_code == status.HTTP_404_NOT_FOUND


def test_get_canonical_rounds(client):
    """Verify /api/quizzes/canonical-rounds returns the 6 agreed modern multi-round types."""
    res = client.get("/api/quizzes/canonical-rounds")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    round_ids = {r["id"] for r in data}
    expected_agreed = {
        "gulmisiz_rayhonmisiz",
        "zanjir",
        "mantiqqasqon",
        "aldama_meni",
        "rasmiyatchilik",
        "vabank"
    }
    assert expected_agreed.issubset(round_ids)


def test_create_quiz_draft_single_round(client, db_session, sample_question_bank):
    """
    Contract: Creating a draft creates Quiz + QuizVersion (status='draft', published_manifest=NULL)
    and attaches questions through RoundQuestion.
    Question.round_id MUST remain NULL.
    """
    q1, q2 = sample_question_bank[0], sample_question_bank[1]

    payload = {
        "title": "Zakovat Bahorgi Kubogi #1",
        "description": "2026-yilgi 1-saralash",
        "game_mode": "modern_multiround",
        "round_type": "zanjir",
        "round_config": {"direction": "forward"},
        "question_ids": [q1.id, q2.id]
    }

    res = client.post("/api/quizzes/draft", json=payload)
    assert res.status_code == status.HTTP_201_CREATED
    data = res.json()
    assert data["success"] is True
    assert data["status"] == "draft"
    assert data["title"] == "Zakovat Bahorgi Kubogi #1"
    assert data["rounds_count"] == 1
    assert data["questions_count"] == 2

    # Verify DB State
    quiz_id = data["quiz_id"]
    version_id = data["version_id"]

    version = db_session.query(QuizVersion).filter(QuizVersion.id == version_id).first()
    assert version is not None
    assert version.status == "draft"
    assert version.published_manifest is None  # MUST NOT be published!
    assert len(version.rounds) == 1

    round_obj = version.rounds[0]
    assert round_obj.round_type == "zanjir"
    assert round_obj.sequence == 1
    assert round_obj.config == {"direction": "forward"}
    assert len(round_obj.round_questions) == 2

    rq1, rq2 = round_obj.round_questions[0], round_obj.round_questions[1]
    assert rq1.question_id == q1.id
    assert rq1.sequence == 1
    assert rq2.question_id == q2.id
    assert rq2.sequence == 2

    # CRITICAL ARCHITECTURAL CHECK:
    # Questions in Question Bank MUST retain round_id = NULL
    db_session.refresh(q1)
    db_session.refresh(q2)
    assert q1.round_id is None, "Question.round_id must remain NULL (decoupled)"
    assert q2.round_id is None, "Question.round_id must remain NULL (decoupled)"


def test_question_reusability_across_multiple_drafts(client, db_session, sample_question_bank):
    """Verify that a single Question can be attached to multiple quiz drafts."""
    q1 = sample_question_bank[0]

    # Draft 1
    res1 = client.post("/api/quizzes/draft", json={
        "title": "Quiz A",
        "round_type": "gulmisiz_rayhonmisiz",
        "question_ids": [q1.id]
    })
    assert res1.status_code == status.HTTP_201_CREATED

    # Draft 2
    res2 = client.post("/api/quizzes/draft", json={
        "title": "Quiz B",
        "round_type": "vabank",
        "question_ids": [q1.id]
    })
    assert res2.status_code == status.HTTP_201_CREATED

    # Question has 2 distinct RoundQuestion links
    rqs = db_session.query(RoundQuestion).filter(RoundQuestion.question_id == q1.id).all()
    assert len(rqs) == 2
    assert rqs[0].round.quiz_version.quiz.title == "Quiz A"
    assert rqs[1].round.quiz_version.quiz.title == "Quiz B"


def test_create_quiz_draft_multiple_rounds(client, db_session, sample_question_bank):
    """Verify creating a draft with multiple explicitly configured canonical rounds."""
    q1, q2, q3 = sample_question_bank[0], sample_question_bank[1], sample_question_bank[2]

    payload = {
        "title": "Multi-Round Championship",
        "game_mode": "modern_multiround",
        "rounds": [
            {
                "sequence": 1,
                "round_type": "gulmisiz_rayhonmisiz",
                "config": {"theme": "Kirish"},
                "question_ids": [q1.id]
            },
            {
                "sequence": 2,
                "round_type": "vabank",
                "config": {"multiplier": 2},
                "question_ids": [q2.id, q3.id]
            }
        ]
    }

    res = client.post("/api/quizzes/draft", json=payload)
    assert res.status_code == status.HTTP_201_CREATED
    data = res.json()
    assert data["rounds_count"] == 2
    assert data["questions_count"] == 3

    version = db_session.query(QuizVersion).filter(QuizVersion.id == data["version_id"]).first()
    assert len(version.rounds) == 2
    assert version.rounds[0].round_type == "gulmisiz_rayhonmisiz"
    assert version.rounds[0].sequence == 1
    assert version.rounds[1].round_type == "vabank"
    assert version.rounds[1].sequence == 2


def test_create_quiz_draft_invalid_round_type(client, sample_question_bank):
    """Verify rejection of unknown or invented round types."""
    q1 = sample_question_bank[0]
    payload = {
        "title": "Invalid Round Quiz",
        "round_type": "invented_fake_type",
        "question_ids": [q1.id]
    }
    res = client.post("/api/quizzes/draft", json=payload)
    assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "Noma'lum raund turi" in res.json()["detail"]


def test_create_quiz_draft_missing_question(client):
    """Verify 404 when referenced question IDs do not exist."""
    payload = {
        "title": "Missing Question Quiz",
        "round_type": "zanjir",
        "question_ids": [999998, 999999]
    }
    res = client.post("/api/quizzes/draft", json=payload)
    assert res.status_code == status.HTTP_404_NOT_FOUND


def test_create_quiz_draft_empty_title_or_questions(client):
    """Verify 422 validation error when title or question_ids is empty."""
    # Empty title
    res1 = client.post("/api/quizzes/draft", json={"title": "   ", "question_ids": [1]})
    assert res1.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # Empty question IDs
    res2 = client.post("/api/quizzes/draft", json={"title": "Valid Title", "question_ids": []})
    assert res2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_draft_quizzes(client, sample_question_bank):
    """Verify GET /api/quizzes/drafts lists all created drafts."""
    q1 = sample_question_bank[0]
    client.post("/api/quizzes/draft", json={
        "title": "Editor Draft One",
        "round_type": "rasmiyatchilik",
        "question_ids": [q1.id]
    })

    res = client.get("/api/quizzes/drafts")
    assert res.status_code == status.HTTP_200_OK
    drafts = res.json()
    assert len(drafts) >= 1
    d = next(item for item in drafts if item["title"] == "Editor Draft One")
    assert d["rounds"][0]["round_type"] == "rasmiyatchilik"
    assert d["total_questions"] == 1


def test_bank_html_page_served(client):
    """Verify GET /bank returns status 200 and renders HTML."""
    res = client.get("/bank")
    assert res.status_code == status.HTTP_200_OK
    assert "text/html" in res.headers["content-type"]
    assert "Savollar Banki" in res.text
