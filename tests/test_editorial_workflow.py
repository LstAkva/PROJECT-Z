import os
import uuid
import pytest
from fastapi import status
from sqlalchemy import distinct

from models import User, Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer
from services.editorial import (
    record_editorial_decision,
    get_question_editorial_status,
    get_question_qa_info,
    DOCUMENTED_REPLACEMENT_CANDIDATES,
)
from api.drafts import run_publish_validation, execute_publish_version


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================

@pytest.fixture
def owner_email_env(monkeypatch):
    """Configures a unique, dynamic test owner email via environment variable."""
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


def seed_classic_2x12_pack(db_session, status_for_all: str = "draft") -> tuple[Quiz, QuizVersion, list[Question]]:
    """Seeds a classic Zakovat pack (2 rounds x 12 questions = 24 questions)."""
    quiz = Quiz(title="Test Classic Zakovat Pack", description="Automated editorial test pack")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        status="draft",
        game_mode="classic_zakovat",
    )
    db_session.add(version)
    db_session.flush()

    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="classic_round_1")
    r2 = Round(quiz_version_id=version.id, sequence=2, round_type="classic_round_2")
    db_session.add_all([r1, r2])
    db_session.flush()

    questions = []
    for i in range(1, 25):
        round_target = r1 if i <= 12 else r2
        round_seq = i if i <= 12 else (i - 12)

        q = Question(
            text=f"Zakovat savoli #{i}: Tarixiy savol matni {i}",
            category="Tarix" if i % 2 == 0 else "Adabiyot",
            status=status_for_all,
            default_points=1,
            points=1,
            question_type="text",
            round_id=None,  # Invariant: Question Bank asset has round_id=NULL
            source_meta={"telegram_pack": "Pack 02", "telegram_message_id": 100 + i},
        )
        db_session.add(q)
        db_session.flush()

        ans = AcceptedAnswer(question_id=q.id, answer_text=f"Javob {i}", is_primary=True)
        db_session.add(ans)

        rq = RoundQuestion(
            round_id=round_target.id,
            question_id=q.id,
            sequence=round_seq,
            points_override=1,
        )
        db_session.add(rq)
        questions.append(q)

    db_session.commit()
    return quiz, version, questions


# =============================================================================
# 1. AUTHORIZATION & ACCESS CONTROL
# =============================================================================

def test_editorial_endpoints_anonymous_unauthorized_401(client, db_session, owner_email_env):
    """Anonymous requests must be rejected with 401 on all editorial endpoints."""
    client.cookies.clear()
    quiz, version, questions = seed_classic_2x12_pack(db_session)
    target_q = questions[0]

    # Question decision
    res = client.post(f"/api/owner/questions/{target_q.id}/decision", json={"decision": "approve"})
    assert res.status_code == status.HTTP_401_UNAUTHORIZED

    # Quiz batch decision
    res = client.post(f"/api/owner/quizzes/{quiz.id}/decision", json={"decision": "approve"})
    assert res.status_code == status.HTTP_401_UNAUTHORIZED

    # Add answer
    res = client.post(f"/api/owner/questions/{target_q.id}/answers", json={"answer_text": "Sinonim"})
    assert res.status_code == status.HTTP_401_UNAUTHORIZED


def test_editorial_endpoints_normal_user_forbidden_403(client, db_session, owner_email_env):
    """Authenticated non-owner users must be rejected with 403 Forbidden."""
    normal_email = f"normal_{uuid.uuid4().hex[:8]}@example.test"
    register_and_login_user(client, normal_email, "Normal Player")

    quiz, version, questions = seed_classic_2x12_pack(db_session)
    target_q = questions[0]

    res = client.post(f"/api/owner/questions/{target_q.id}/decision", json={"decision": "approve"})
    assert res.status_code == status.HTTP_403_FORBIDDEN

    res = client.post(f"/api/owner/quizzes/{quiz.id}/decision", json={"decision": "approve"})
    assert res.status_code == status.HTTP_403_FORBIDDEN

    res = client.post(f"/api/owner/questions/{target_q.id}/answers", json={"answer_text": "Sinonim"})
    assert res.status_code == status.HTTP_403_FORBIDDEN


# =============================================================================
# 2. EDITORIAL DECISION WORKFLOW (Approve / Reject / Needs Review)
# =============================================================================

def test_single_question_editorial_decisions(client, db_session, owner_email_env):
    """Owner can approve, reject, or mark needs_review with metadata."""
    register_and_login_user(client, owner_email_env, "Test Owner")
    quiz, version, questions = seed_classic_2x12_pack(db_session)
    q = questions[0]

    # 1. Approve
    res = client.post(f"/api/owner/questions/{q.id}/decision", json={
        "decision": "approve",
        "notes": "Tasdiqlandi: to'liq mos keladi",
    })
    assert res.status_code == status.HTTP_200_OK, res.text
    data = res.json()
    assert data["success"] is True
    assert data["editorial_status"] == "approved"
    assert data["status"] == "approved"
    assert data["editorial"]["decision"] == "approved"
    assert data["editorial"]["decided_by"] == owner_email_env
    assert "Tasdiqlandi" in data["editorial"]["notes"]

    # 2. Needs Review
    res = client.post(f"/api/owner/questions/{q.id}/decision", json={
        "decision": "needs_review",
        "notes": "Imlo xatosi bor, tekshirish kerak",
    })
    assert res.status_code == status.HTTP_200_OK
    assert res.json()["editorial_status"] == "needs_review"
    assert res.json()["status"] == "needs_review"

    # 3. Reject
    res = client.post(f"/api/owner/questions/{q.id}/decision", json={
        "decision": "reject",
        "notes": "Savolda faktik xatolik mavjud",
    })
    assert res.status_code == status.HTTP_200_OK
    assert res.json()["editorial_status"] == "rejected"
    assert res.json()["status"] == "rejected"

    # 4. Invalid decision rejected with 400 Bad Request
    res = client.post(f"/api/owner/questions/{q.id}/decision", json={
        "decision": "arbitrary_invalid_state",
    })
    assert res.status_code == status.HTTP_400_BAD_REQUEST


def test_batch_editorial_decision_quiz(client, db_session, owner_email_env):
    """Owner can batch approve all questions in a pack."""
    register_and_login_user(client, owner_email_env, "Test Owner")
    quiz, version, questions = seed_classic_2x12_pack(db_session, status_for_all="draft")

    # Initially all 24 questions are unresolved drafts
    res = client.get(f"/api/owner/quizzes/{quiz.id}")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["readiness_state"] == "Draft"
    assert data["is_ready_to_publish"] is False
    assert data["editorial_summary"]["unresolved"] == 24
    assert data["editorial_summary"]["approved"] == 0

    # Batch approve all 24 questions
    batch_res = client.post(f"/api/owner/quizzes/{quiz.id}/decision", json={
        "decision": "approve",
        "notes": "Owner tomonidan ommaviy tasdiqlandi",
    })
    assert batch_res.status_code == status.HTTP_200_OK
    bdata = batch_res.json()
    assert bdata["updated_question_count"] == 24
    assert bdata["editorial_summary"]["approved"] == 24
    assert bdata["editorial_summary"]["unresolved"] == 0
    assert bdata["readiness_state"] == "Ready to Publish"
    assert bdata["is_ready_to_publish"] is True


# =============================================================================
# 3. ACCEPTED ANSWER & SYNONYM MANAGEMENT
# =============================================================================

def test_accepted_answer_management(client, db_session, owner_email_env):
    """Owner can add synonyms, change primary answer, and delete non-primary answers."""
    register_and_login_user(client, owner_email_env, "Test Owner")
    quiz, version, questions = seed_classic_2x12_pack(db_session)
    q = questions[0]

    # 1. Add Synonym
    res = client.post(f"/api/owner/questions/{q.id}/answers", json={
        "answer_text": "Muqobil Javob Varianti",
        "is_primary": False,
    })
    assert res.status_code == status.HTTP_201_CREATED
    new_ans_id = res.json()["answer_id"]
    assert res.json()["is_primary"] is False

    # Verify detail shows the new synonym
    res_q = client.get(f"/api/owner/questions/{q.id}")
    assert len(res_q.json()["accepted_answers"]) == 2

    # 2. Change Primary Answer
    res_prim = client.put(f"/api/owner/questions/{q.id}/answers/{new_ans_id}/primary")
    assert res_prim.status_code == status.HTTP_200_OK
    assert res_prim.json()["primary_answer_id"] == new_ans_id

    # Verify primary changed in detail
    res_q = client.get(f"/api/owner/questions/{q.id}")
    assert res_q.json()["primary_answer"] == "Muqobil Javob Varianti"

    # 3. Delete non-primary answer (the original answer is now non-primary)
    orig_ans_id = [a["id"] for a in res_q.json()["accepted_answers"] if not a["is_primary"]][0]
    res_del = client.delete(f"/api/owner/questions/{q.id}/answers/{orig_ans_id}")
    assert res_del.status_code == status.HTTP_200_OK

    # 4. Guard: cannot delete the only remaining answer
    res_del_last = client.delete(f"/api/owner/questions/{q.id}/answers/{new_ans_id}")
    assert res_del_last.status_code == status.HTTP_400_BAD_REQUEST
    assert "kamida bitta" in res_del_last.json()["detail"].lower()


# =============================================================================
# 4. QUESTION REPLACEMENT & ARCHITECTURAL INVARIANTS
# =============================================================================

def test_question_replacement_preserves_bank_invariant(client, db_session, owner_email_env):
    """
    Replacing a question in a pack modifies RoundQuestion.question_id only.
    The original question in Question Bank remains untouched with round_id=NULL.
    """
    register_and_login_user(client, owner_email_env, "Test Owner")
    quiz, version, questions = seed_classic_2x12_pack(db_session)

    # Create candidate replacement in Question Bank
    candidate = Question(
        text="Candidate replacement: Amir Temur haykali qaysi shaharda?",
        category="Tarix",
        status="approved",
        default_points=1,
        points=1,
        question_type="text",
        round_id=None,
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add(AcceptedAnswer(question_id=candidate.id, answer_text="Shahrisabz", is_primary=True))
    db_session.commit()

    round_1 = version.rounds[0]
    rq = round_1.round_questions[0]
    old_q_id = rq.question_id

    # Replace Q1 with candidate
    res = client.post(
        f"/api/owner/quizzes/{quiz.id}/rounds/{round_1.id}/questions/{rq.id}/replace",
        json={"new_question_id": candidate.id},
    )
    assert res.status_code == status.HTTP_200_OK
    assert res.json()["new_question_id"] == candidate.id

    # Verify RoundQuestion updated
    db_session.refresh(rq)
    assert rq.question_id == candidate.id

    # Verify original question is still in Question Bank and untouched
    old_q = db_session.query(Question).filter(Question.id == old_q_id).first()
    assert old_q is not None
    assert old_q.round_id is None
    assert "Zakovat savoli #1" in old_q.text


# =============================================================================
# 5. PACK PUBLISH-READINESS VALIDATION & PUBLISH GATE
# =============================================================================

def test_pack_publish_readiness_states_and_publish_gate(client, db_session, owner_email_env):
    """
    Derived readiness states:
    - 24 unresolved drafts -> Draft (NOT READY)
    - 1 rejected question -> Draft (NOT READY)
    - 24 approved questions -> Ready to Publish
    - Publish gate fails closed (422) if not ready
    - Publish gate succeeds when Ready to Publish, freezing published_manifest
    """
    register_and_login_user(client, owner_email_env, "Test Owner")
    quiz, version, questions = seed_classic_2x12_pack(db_session, status_for_all="draft")

    # 1. Initial state: all 24 are unresolved drafts -> NOT READY
    val = run_publish_validation(version, db_session)
    assert val["is_ready_to_publish"] is False
    assert val["readiness_state"] == "Draft"
    assert val["editorial_summary"]["unresolved"] == 24

    # Publish attempt fails closed with 422
    res_pub = client.post(f"/api/owner/quizzes/{quiz.id}/publish", json={"visibility": "public"})
    assert res_pub.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    err_detail = res_pub.json()["detail"]
    assert "xatolar mavjud" in err_detail["message"].lower()
    assert len(err_detail["errors"]) == 24

    # 2. Owner approves 23 questions, but 1 is rejected -> NOT READY
    for q in questions[:23]:
        record_editorial_decision(q, "approve", owner_email_env)
    record_editorial_decision(questions[23], "reject", owner_email_env)
    db_session.commit()

    val = run_publish_validation(version, db_session)
    assert val["is_ready_to_publish"] is False
    assert val["readiness_state"] == "Draft"
    assert any("rad etilgan" in e.lower() for e in val["errors"])

    # 3. Owner approves all 24 questions -> READY TO PUBLISH
    record_editorial_decision(questions[23], "approve", owner_email_env)
    db_session.commit()

    val = run_publish_validation(version, db_session)
    assert val["valid"] is True
    assert val["is_ready_to_publish"] is True
    assert val["readiness_state"] == "Ready to Publish"
    assert val["editorial_summary"]["approved"] == 24
    assert val["editorial_summary"]["unresolved"] == 0
    assert val["editorial_summary"]["rejected"] == 0

    # 4. Authoritative publish succeeds
    res_pub_success = client.post(f"/api/owner/quizzes/{quiz.id}/publish", json={"visibility": "public"})
    assert res_pub_success.status_code == status.HTTP_200_OK
    pub_data = res_pub_success.json()
    assert pub_data["status"] == "published"

    # Verify published version has frozen manifest
    db_session.refresh(version)
    assert version.status == "published"
    assert version.published_manifest is not None
    assert len(version.published_manifest["rounds"]) == 2
    total_manifest_qs = sum(len(r["questions"]) for r in version.published_manifest["rounds"])
    assert total_manifest_qs == 24
