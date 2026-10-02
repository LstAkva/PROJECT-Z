import os
import uuid
import pytest
from fastapi import status
from sqlalchemy import distinct
from models import User, Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer
from api.drafts import execute_publish_version


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================

@pytest.fixture
def owner_email_env(monkeypatch):
    """
    Configures a unique, dynamic test owner email via environment variable.
    Ensures no hardcoded owner email addresses in application code or tests.
    """
    dynamic_owner = f"test_owner_{uuid.uuid4().hex[:8]}@example.test"
    monkeypatch.setenv("OWNER_EMAIL", dynamic_owner)
    return dynamic_owner


def register_and_login_user(client, email: str, display_name: str = "Test User") -> User:
    """Helper to register and authenticate a user in the test client session."""
    client.cookies.clear()
    res = client.post("/api/auth/register", json={
        "email": email,
        "password": "ValidPassword123!",
        "display_name": display_name,
    })
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()["user"]


def seed_24_bank_questions(db_session) -> list[Question]:
    """Seeds 24 ready Question Bank questions for Zakovat 2x12 tests."""
    questions = []
    for i in range(1, 25):
        q = Question(
            text=f"Bank Savol {i}: Test savol matni {i}",
            category="Adabiyot" if i % 2 == 0 else "Tarix",
            status="approved",
            default_points=1,
            points=1,
            question_type="text",
            round_id=None,  # MUST remain NULL
        )
        db_session.add(q)
        db_session.flush()

        ans_prim = AcceptedAnswer(question_id=q.id, answer_text=f"Javob {i}", is_primary=True)
        ans_alt = AcceptedAnswer(question_id=q.id, answer_text=f"Muqobil {i}", is_primary=False)
        db_session.add_all([ans_prim, ans_alt])
        questions.append(q)

    db_session.commit()
    return questions


# =============================================================================
# 1. OWNER AUTHORIZATION TESTS
# =============================================================================

def test_owner_auth_anonymous_rejected_401(client, owner_email_env):
    """Anonymous visitors must receive HTTP 401 Unauthorized on all owner endpoints."""
    client.cookies.clear()
    res_status = client.get("/api/owner/status")
    assert res_status.status_code == status.HTTP_401_UNAUTHORIZED

    res_quizzes = client.get("/api/owner/quizzes")
    assert res_quizzes.status_code == status.HTTP_401_UNAUTHORIZED

    res_create = client.post("/api/owner/quizzes", json={"title": "Pack"})
    assert res_create.status_code == status.HTTP_401_UNAUTHORIZED

    res_questions = client.get("/api/owner/questions")
    assert res_questions.status_code == status.HTTP_401_UNAUTHORIZED


def test_owner_auth_normal_user_rejected_403(client, owner_email_env):
    """Authenticated users whose email does not match OWNER_EMAIL must receive HTTP 403 Forbidden."""
    normal_email = f"normal_player_{uuid.uuid4().hex[:8]}@example.test"
    register_and_login_user(client, normal_email, "Normal Player")

    res_status = client.get("/api/owner/status")
    assert res_status.status_code == status.HTTP_403_FORBIDDEN
    assert "owner" in res_status.json()["detail"].lower()

    res_quizzes = client.get("/api/owner/quizzes")
    assert res_quizzes.status_code == status.HTTP_403_FORBIDDEN

    res_create = client.post("/api/owner/quizzes", json={"title": "Unauthorized Pack"})
    assert res_create.status_code == status.HTTP_403_FORBIDDEN


def test_owner_auth_missing_owner_email_fails_closed_403(client, monkeypatch):
    """When neither OWNER_EMAIL nor OWNER_EMAILS is configured, access must FAIL CLOSED with HTTP 403."""
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    monkeypatch.delenv("OWNER_EMAILS", raising=False)

    any_user_email = f"user_{uuid.uuid4().hex[:8]}@example.test"
    register_and_login_user(client, any_user_email, "Any User")

    res = client.get("/api/owner/status")
    assert res.status_code == status.HTTP_403_FORBIDDEN
    assert "konfiguratsiyasi mavjud emas" in res.json()["detail"].lower()


def test_owner_auth_configured_owner_authorized_200(client, owner_email_env):
    """Authenticated user matching configured OWNER_EMAIL receives HTTP 200 on owner endpoints."""
    register_and_login_user(client, owner_email_env, "Configured Owner")

    res = client.get("/api/owner/status")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["status"] == "online"
    assert data["owner_email"] == owner_email_env
    assert "metrics" in data
    # Verify no credentials leaked
    assert "password" not in data
    assert "DATABASE_URL" not in data


def test_owner_auth_supports_plural_owner_emails(client, monkeypatch):
    """Verifies that OWNER_EMAILS (comma-separated plural) is supported."""
    dynamic_owner1 = f"owner1_{uuid.uuid4().hex[:8]}@example.test"
    dynamic_owner2 = f"owner2_{uuid.uuid4().hex[:8]}@example.test"
    monkeypatch.setenv("OWNER_EMAILS", f"{dynamic_owner1}, {dynamic_owner2}")

    # Owner 2 logs in
    register_and_login_user(client, dynamic_owner2, "Owner Two")
    res = client.get("/api/owner/status")
    assert res.status_code == status.HTTP_200_OK
    assert res.json()["owner_email"] == dynamic_owner2


# =============================================================================
# 2. QUIZ OPERATIONS TESTS
# =============================================================================

def test_create_zakovat_draft_initializes_two_canonical_rounds(client, owner_email_env, db_session):
    """Creating classic_zakovat draft automatically creates 1-tur and 2-tur."""
    register_and_login_user(client, owner_email_env, "Owner")

    res = client.post("/api/owner/quizzes", json={
        "title": "Zakovat Yangi To'plam #1",
        "description": "24 ta original savollar",
        "game_mode": "classic_zakovat",
    })
    assert res.status_code == status.HTTP_201_CREATED, res.text
    quiz_id = res.json()["quiz_id"]

    # Verify rounds in detail endpoint
    detail_res = client.get(f"/api/owner/quizzes/{quiz_id}")
    assert detail_res.status_code == status.HTTP_200_OK
    detail = detail_res.json()
    assert detail["title"] == "Zakovat Yangi To'plam #1"
    assert detail["status"] == "draft"
    assert detail["game_mode"] == "classic_zakovat"
    assert len(detail["rounds"]) == 2

    r1, r2 = detail["rounds"][0], detail["rounds"][1]
    assert r1["sequence"] == 1 and r1["round_title"] == "1-tur"
    assert r2["sequence"] == 2 and r2["round_title"] == "2-tur"


def test_update_draft_quiz_metadata(client, owner_email_env):
    """Owner can update draft title and description."""
    register_and_login_user(client, owner_email_env, "Owner")

    create_res = client.post("/api/owner/quizzes", json={"title": "Eski Nom"})
    quiz_id = create_res.json()["quiz_id"]

    up_res = client.put(f"/api/owner/quizzes/{quiz_id}", json={
        "title": "Yangi Yangilangan Nom",
        "description": "Yangi tavsif",
    })
    assert up_res.status_code == status.HTTP_200_OK
    assert up_res.json()["title"] == "Yangi Yangilangan Nom"
    assert up_res.json()["description"] == "Yangi tavsif"


def test_delete_draft_quiz_succeeds_if_never_published(client, owner_email_env, db_session):
    """Draft quiz that has never been published can be safely deleted."""
    register_and_login_user(client, owner_email_env, "Owner")

    create_res = client.post("/api/owner/quizzes", json={"title": "O'chiriladigan Qoralama"})
    quiz_id = create_res.json()["quiz_id"]

    del_res = client.delete(f"/api/owner/quizzes/{quiz_id}")
    assert del_res.status_code == status.HTTP_200_OK

    # Confirm it no longer exists
    get_res = client.get(f"/api/owner/quizzes/{quiz_id}")
    assert get_res.status_code == status.HTTP_404_NOT_FOUND


def test_delete_published_quiz_strictly_rejected(client, owner_email_env, db_session):
    """A quiz that has ever been published cannot be destructively deleted."""
    register_and_login_user(client, owner_email_env, "Owner")

    # Create and publish quiz
    quiz = Quiz(title="Nashr Qilingan Viktorina")
    db_session.add(quiz)
    db_session.flush()
    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        status="published",
        game_mode="classic_zakovat",
        published_manifest={"rounds": []},
    )
    db_session.add(version)
    db_session.commit()

    del_res = client.delete(f"/api/owner/quizzes/{quiz.id}")
    assert del_res.status_code == status.HTTP_400_BAD_REQUEST
    assert "tarixiy ma'lumotlarni saqlash" in del_res.json()["detail"].lower()

    # Archive instead succeeds
    arch_res = client.post(f"/api/owner/quizzes/{quiz.id}/archive")
    assert arch_res.status_code == status.HTTP_200_OK
    db_session.refresh(version)
    assert version.status == "archived"


# =============================================================================
# 3. QUESTION BANK ATTACH, REMOVE, REORDER, REPLACE OPERATIONS
# =============================================================================

def test_attach_question_and_preserve_question_bank(client, owner_email_env, db_session):
    """Attaching a Question Bank question adds RoundQuestion and keeps Question.round_id NULL."""
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    create_res = client.post("/api/owner/quizzes", json={"title": "Attach Test Pack"})
    quiz_id = create_res.json()["quiz_id"]
    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_id = detail["rounds"][0]["round_id"]

    # Attach Question 1
    q1 = bank_qs[0]
    attach_res = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={
        "question_id": q1.id
    })
    assert attach_res.status_code == status.HTTP_201_CREATED, attach_res.text
    rq_id = attach_res.json()["round_question_id"]
    assert attach_res.json()["sequence"] == 1

    # Invariant: Question.round_id MUST remain NULL in Question Bank
    db_session.refresh(q1)
    assert q1.round_id is None, "Question Bank item must never have round_id set directly!"


def test_attach_question_duplicate_in_same_quiz_rejected(client, owner_email_env, db_session):
    """Accidental duplicate attachment within the same quiz is rejected."""
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    create_res = client.post("/api/owner/quizzes", json={"title": "Duplicate Pack"})
    quiz_id = create_res.json()["quiz_id"]
    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_id = detail["rounds"][0]["round_id"]
    r2_id = detail["rounds"][1]["round_id"]

    q1 = bank_qs[0]
    client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": q1.id})

    # Try attaching q1 again to round 2 of the same quiz
    dup_res = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r2_id}/attach-question", json={"question_id": q1.id})
    assert dup_res.status_code == status.HTTP_400_BAD_REQUEST
    assert "allaqachon biriktirilgan" in dup_res.json()["detail"].lower()


def test_question_can_be_reused_across_different_quizzes(client, owner_email_env, db_session):
    """A Question Bank question MAY be reused across different quiz packs."""
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)
    q1 = bank_qs[0]

    # Pack 1
    p1 = client.post("/api/owner/quizzes", json={"title": "Pack 1"}).json()
    r1_p1 = client.get(f"/api/owner/quizzes/{p1['quiz_id']}").json()["rounds"][0]["round_id"]
    att1 = client.post(f"/api/owner/quizzes/{p1['quiz_id']}/rounds/{r1_p1}/attach-question", json={"question_id": q1.id})
    assert att1.status_code == status.HTTP_201_CREATED

    # Pack 2
    p2 = client.post("/api/owner/quizzes", json={"title": "Pack 2"}).json()
    r1_p2 = client.get(f"/api/owner/quizzes/{p2['quiz_id']}").json()["rounds"][0]["round_id"]
    att2 = client.post(f"/api/owner/quizzes/{p2['quiz_id']}/rounds/{r1_p2}/attach-question", json={"question_id": q1.id})
    assert att2.status_code == status.HTTP_201_CREATED, "Question Bank question must be reusable across quizzes!"


def test_remove_question_from_round_reindexes_contiguously(client, owner_email_env, db_session):
    """Removing RoundQuestion deletes link and re-indexes remaining questions 1..N."""
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    p = client.post("/api/owner/quizzes", json={"title": "Reindex Pack"}).json()
    quiz_id = p["quiz_id"]
    r1_id = client.get(f"/api/owner/quizzes/{quiz_id}").json()["rounds"][0]["round_id"]

    rq1 = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[0].id}).json()["round_question_id"]
    rq2 = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[1].id}).json()["round_question_id"]
    rq3 = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[2].id}).json()["round_question_id"]

    # Delete rq2 (middle question)
    del_res = client.delete(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/questions/{rq2}")
    assert del_res.status_code == status.HTTP_200_OK

    # Verify rq1 is sequence 1 and rq3 is now sequence 2
    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_qs = detail["rounds"][0]["questions"]
    assert len(r1_qs) == 2
    assert r1_qs[0]["round_question_id"] == rq1 and r1_qs[0]["round_sequence"] == 1
    assert r1_qs[1]["round_question_id"] == rq3 and r1_qs[1]["round_sequence"] == 2

    # Invariant: Question Bank item for rq2 is still alive
    assert db_session.query(Question).filter(Question.id == bank_qs[1].id).first() is not None


def test_reorder_round_questions(client, owner_email_env, db_session):
    """PUT reorder updates sequences within the round."""
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    p = client.post("/api/owner/quizzes", json={"title": "Reorder Pack"}).json()
    quiz_id = p["quiz_id"]
    r1_id = client.get(f"/api/owner/quizzes/{quiz_id}").json()["rounds"][0]["round_id"]

    rq1 = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[0].id}).json()["round_question_id"]
    rq2 = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[1].id}).json()["round_question_id"]

    # Reverse order: [rq2, rq1]
    reorder_res = client.put(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/questions/reorder", json={
        "ordered_rq_ids": [rq2, rq1]
    })
    assert reorder_res.status_code == status.HTTP_200_OK

    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_qs = detail["rounds"][0]["questions"]
    assert r1_qs[0]["round_question_id"] == rq2 and r1_qs[0]["round_sequence"] == 1
    assert r1_qs[1]["round_question_id"] == rq1 and r1_qs[1]["round_sequence"] == 2


def test_replace_question_in_round(client, owner_email_env, db_session):
    """Replacing question updates question_id without mutating Question Bank."""
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    p = client.post("/api/owner/quizzes", json={"title": "Replace Pack"}).json()
    quiz_id = p["quiz_id"]
    r1_id = client.get(f"/api/owner/quizzes/{quiz_id}").json()["rounds"][0]["round_id"]

    rq1 = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[0].id}).json()["round_question_id"]

    # Replace bank_qs[0] with bank_qs[1]
    rep_res = client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/questions/{rq1}/replace", json={
        "new_question_id": bank_qs[1].id
    })
    assert rep_res.status_code == status.HTTP_200_OK
    assert rep_res.json()["new_question_id"] == bank_qs[1].id

    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    assert detail["rounds"][0]["questions"][0]["question_id"] == bank_qs[1].id


# =============================================================================
# 4. VALIDATION & PUBLISHING TESTS
# =============================================================================

def test_validation_and_publishing_complete_zakovat_2x12(client, owner_email_env, db_session):
    """
    Complete 2x12 Zakovat workflow:
    1. Incomplete pack fails validation.
    2. Attach exactly 12 questions to 1-tur and 12 to 2-tur.
    3. Validate passes.
    4. Publish freezes manifest and lists quiz in public Arena (/api/quizzes).
    """
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    p = client.post("/api/owner/quizzes", json={"title": "Zakovat 2x12 Full Test"}).json()
    quiz_id = p["quiz_id"]
    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_id = detail["rounds"][0]["round_id"]
    r2_id = detail["rounds"][1]["round_id"]

    # 1. Incomplete validation fails
    val1 = client.post(f"/api/owner/quizzes/{quiz_id}/validate").json()
    assert val1["valid"] is False
    assert any("1-turda aynan 12 ta savol" in e for e in val1["errors"])

    # Attempt to publish incomplete pack fails with HTTP 422
    pub_fail = client.post(f"/api/owner/quizzes/{quiz_id}/publish")
    assert pub_fail.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # 2. Attach 12 questions to 1-tur (questions 0..11)
    for i in range(12):
        client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[i].id})

    # Still incomplete (round 2 has 0)
    val2 = client.post(f"/api/owner/quizzes/{quiz_id}/validate").json()
    assert val2["valid"] is False
    assert any("2-turda aynan 12 ta savol" in e for e in val2["errors"])

    # Attach 12 questions to 2-tur (questions 12..23)
    for i in range(12, 24):
        client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r2_id}/attach-question", json={"question_id": bank_qs[i].id})

    # 3. Complete validation passes
    val3 = client.post(f"/api/owner/quizzes/{quiz_id}/validate").json()
    assert val3["valid"] is True, f"Errors: {val3['errors']}"

    # Verify global numbering in detail endpoint
    full_detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    assert full_detail["total_questions"] == 24
    r1_qs = full_detail["rounds"][0]["questions"]
    r2_qs = full_detail["rounds"][1]["questions"]
    assert r1_qs[0]["global_sequence"] == 1
    assert r1_qs[11]["global_sequence"] == 12
    assert r2_qs[0]["global_sequence"] == 13
    assert r2_qs[11]["global_sequence"] == 24

    # 4. Publish pack
    pub_res = client.post(f"/api/owner/quizzes/{quiz_id}/publish", json={"category": "Zakovat"})
    assert pub_res.status_code == status.HTTP_200_OK
    assert pub_res.json()["status"] == "published"

    # Public Arena sees the published pack!
    arena_res = client.get("/api/quizzes")
    assert arena_res.status_code == status.HTTP_200_OK
    arena_quizzes = arena_res.json()
    assert any(q["quiz_id"] == quiz_id for q in arena_quizzes)


# =============================================================================
# 5. PUBLISHED SNAPSHOT IMMUTABILITY TEST
# =============================================================================

def test_published_snapshot_immutability_when_bank_question_mutated(client, owner_email_env, db_session):
    """
    CRITICAL ARCHITECTURAL INVARIANT:
    After publication, modifying a Question Bank question does NOT alter the published quiz representation.
    """
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    p = client.post("/api/owner/quizzes", json={"title": "Immutable Snapshot Test"}).json()
    quiz_id = p["quiz_id"]
    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_id = detail["rounds"][0]["round_id"]
    r2_id = detail["rounds"][1]["round_id"]

    for i in range(12):
        client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[i].id})
    for i in range(12, 24):
        client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r2_id}/attach-question", json={"question_id": bank_qs[i].id})

    # Publish
    pub_res = client.post(f"/api/owner/quizzes/{quiz_id}/publish")
    assert pub_res.status_code == status.HTTP_200_OK

    # Question 1 original text
    q1 = bank_qs[0]
    original_text = q1.text

    # Verify Arena / public detail shows original_text
    quiz_arena_data = client.get(f"/api/quizzes/{quiz_id}").json()
    assert quiz_arena_data["rounds"][0]["questions"][0]["text"] == original_text

    # NOW MUTATE QUESTION BANK QUESTION DIRECTLY IN DATABASE
    q1.text = "BU BUTUNLAY O'ZGARTIRILGAN MATN (BANK MUTATION)"
    db_session.commit()

    # Public Arena quiz detail MUST STILL return original_text from frozen published_manifest!
    quiz_arena_after = client.get(f"/api/quizzes/{quiz_id}").json()
    assert quiz_arena_after["rounds"][0]["questions"][0]["text"] == original_text
    assert quiz_arena_after["rounds"][0]["questions"][0]["text"] != "BU BUTUNLAY O'ZGARTIRILGAN MATN (BANK MUTATION)"


# =============================================================================
# 6. READ-ONLY VALIDATION FOR PUBLISHED AND DRAFT QUIZZES (REGRESSION TESTS)
# =============================================================================

def test_published_pack02_validation_returns_normal_validation_response_and_does_not_mutate(client, owner_email_env, db_session):
    """
    Regression test:
    Validating published Pack 02 via POST /api/owner/quizzes/{quiz_id}/validate
    must return a normal validation response schema (HTTP 200, valid: True, errors: []),
    and must NEVER mutate the published version or manifest.
    """
    from scripts.seed_and_publish_pack02_prod import seed_and_publish_pack02

    register_and_login_user(client, owner_email_env, "Owner")
    seed_result = seed_and_publish_pack02(db_session, dry_run=False)
    quiz_id = seed_result.get("quiz_id")
    if not quiz_id:
        quiz = db_session.query(Quiz).filter(Quiz.title == "Classic Zakovat — Pack 02").first()
        quiz_id = quiz.id

    # Record published version state before validation
    pub_v_before = (
        db_session.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "published")
        .first()
    )
    assert pub_v_before is not None
    v_id_before = pub_v_before.id
    v_num_before = pub_v_before.version_number
    v_manifest_before = dict(pub_v_before.published_manifest)
    v_pub_at_before = pub_v_before.published_at

    # Execute validation
    res = client.post(f"/api/owner/quizzes/{quiz_id}/validate")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()

    # Must match authoritative run_publish_validation schema
    assert "valid" in data
    assert data["valid"] is True
    assert "is_ready_to_publish" in data
    assert data["is_ready_to_publish"] is True
    assert data["readiness_state"] == "Published"
    assert "editorial_summary" in data
    assert data["editorial_summary"].get("approved") == 24
    assert data["editorial_summary"].get("total") == 24
    assert "errors" in data
    assert data["errors"] == []
    assert data["total_rounds"] == 2
    assert data["total_questions"] == 24

    # Critical Invariant: Zero mutations to published version
    db_session.expire_all()
    pub_v_after = db_session.query(QuizVersion).filter(QuizVersion.id == v_id_before).first()
    assert pub_v_after.status == "published"
    assert pub_v_after.version_number == v_num_before
    assert pub_v_after.published_manifest == v_manifest_before
    assert pub_v_after.published_at == v_pub_at_before

    # Ensure no spurious draft version was created
    drafts_count = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "draft").count()
    assert drafts_count == 0


def test_draft_validation_still_works_and_prefers_draft_over_published(client, owner_email_env, db_session):
    """
    Regression test:
    Draft validation continues to work. When both a draft and published version exist,
    the validate endpoint prefers the draft version.
    """
    register_and_login_user(client, owner_email_env, "Owner")
    bank_qs = seed_24_bank_questions(db_session)

    # 1. Create a draft quiz
    create_res = client.post("/api/owner/quizzes", json={"title": "Draft Validation Test"})
    quiz_id = create_res.json()["quiz_id"]
    detail = client.get(f"/api/owner/quizzes/{quiz_id}").json()
    r1_id = detail["rounds"][0]["round_id"]
    r2_id = detail["rounds"][1]["round_id"]

    # Incomplete draft validation fails
    val_incomplete = client.post(f"/api/owner/quizzes/{quiz_id}/validate").json()
    assert val_incomplete["valid"] is False
    assert len(val_incomplete["errors"]) > 0

    # Attach all 24 questions
    for i in range(12):
        client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r1_id}/attach-question", json={"question_id": bank_qs[i].id})
    for i in range(12, 24):
        client.post(f"/api/owner/quizzes/{quiz_id}/rounds/{r2_id}/attach-question", json={"question_id": bank_qs[i].id})

    # Complete draft validation passes
    val_complete = client.post(f"/api/owner/quizzes/{quiz_id}/validate").json()
    assert val_complete["valid"] is True
    assert val_complete["readiness_state"] == "Ready to Publish"

    # Publish version 1
    client.post(f"/api/owner/quizzes/{quiz_id}/publish")

    # Add a new incomplete draft version 2 for the same quiz
    v2 = QuizVersion(
        quiz_id=quiz_id,
        version_number=2,
        status="draft",
        game_mode="classic_zakovat",
    )
    db_session.add(v2)
    db_session.commit()

    # Validate MUST evaluate the draft version 2 (which is incomplete), not version 1
    val_v2 = client.post(f"/api/owner/quizzes/{quiz_id}/validate").json()
    assert val_v2["valid"] is False
    assert any("kamida bitta raund" in e or "raundida hech qanday savol yo'q" in e or "2 ta tur" in e for e in val_v2["errors"])


def test_frontend_render_validation_card_and_trigger_safely_handle_error_payloads(client):
    """
    Regression test:
    Validates that templates/owner.html JavaScript functions renderValidationCard()
    and triggerValidateDraft() safely handle error payloads, undefined .errors,
    and HTTP 4xx responses without throwing unhandled TypeErrors.
    """
    import re
    with open("templates/owner.html", "r", encoding="utf-8") as f:
        html = f.read()

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(options=options)

    try:
        driver.get("about:blank")
        driver.execute_script("""
            document.body.innerHTML = `
                <div id="editor-readiness-badge"></div>
                <div id="editor-validation-badge"></div>
                <div id="editor-validation-errors-box"></div>
                <div id="pill-approved"></div>
                <div id="pill-needs-review"></div>
                <div id="pill-rejected"></div>
                <div id="pill-unresolved"></div>
                <div id="chk-r1-count"></div>
                <div id="chk-r2-count"></div>
                <div id="chk-rounds"></div>
                <div id="r1-count-badge"></div>
                <div id="r2-count-badge"></div>
            `;
            window.tailwind = { config: {} };
            window.state = { editorQuizDetail: null };
            window.escapeHtml = function(s) { return s; };
        """)

        scripts = re.findall(r"<script(?:\s+[^>]*)?>(.*?)</script>", html, re.DOTALL)
        for s in scripts:
            if s.strip():
                driver.execute_script("""
                    var scriptEl = document.createElement('script');
                    scriptEl.textContent = arguments[0];
                    document.head.appendChild(scriptEl);
                """, s)

        # 1. Error payload from FastAPI (e.g. {"detail": "Qoralama versiya topilmadi"})
        res1 = driver.execute_script("""
            try {
                renderValidationCard({ detail: "Qoralama versiya topilmadi" });
                return { ok: true, badge: document.getElementById('editor-validation-badge').innerText };
            } catch(e) {
                return { ok: false, error: e.name + ': ' + e.message };
            }
        """)
        assert res1["ok"] is True, f"renderValidationCard threw on error detail payload: {res1.get('error')}"
        assert "0 ta xatolik" in res1["badge"]

        # 2. Empty payload
        res2 = driver.execute_script("""
            try {
                renderValidationCard({});
                return { ok: true, badge: document.getElementById('editor-validation-badge').innerText };
            } catch(e) {
                return { ok: false, error: e.name + ': ' + e.message };
            }
        """)
        assert res2["ok"] is True

        # 3. Payload with valid: false but missing errors array
        res3 = driver.execute_script("""
            try {
                renderValidationCard({ valid: false });
                return { ok: true, badge: document.getElementById('editor-validation-badge').innerText };
            } catch(e) {
                return { ok: false, error: e.name + ': ' + e.message };
            }
        """)
        assert res3["ok"] is True

        # 4. Null / undefined payload
        res4 = driver.execute_script("""
            try {
                renderValidationCard(null);
                renderValidationCard(undefined);
                return { ok: true };
            } catch(e) {
                return { ok: false, error: e.name + ': ' + e.message };
            }
        """)
        assert res4["ok"] is True

        # 5. triggerValidateDraft with mocked 400 response
        res5 = driver.execute_async_script("""
            var done = arguments[arguments.length - 1];
            var lastAlert = null;
            window.alert = function(msg) { lastAlert = msg; };
            state.currentEditorQuizId = 999;
            window.fetch = function() {
                return Promise.resolve({
                    ok: false,
                    status: 400,
                    json: function() { return Promise.resolve({ detail: "Qoralama versiya topilmadi" }); }
                });
            };
            triggerValidateDraft().then(function() {
                done({ ok: true, alert: lastAlert });
            }).catch(function(e) {
                done({ ok: false, error: e.name + ': ' + e.message });
            });
        """)
        assert res5["ok"] is True, f"triggerValidateDraft threw exception: {res5.get('error')}"
        assert "Qoralama versiya topilmadi" in (res5.get("alert") or "")

    finally:
        driver.quit()

