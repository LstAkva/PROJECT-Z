"""
Comprehensive Test Suite for Pack Generator v1
Tests:
- Question eligibility filtering (media handouts, answer cleanliness, editorial status)
- Text normalization & duplicate detection (Uzbek apostrophes, casing, punctuation)
- Deterministic seeded selection & reproducibility
- Classic Zakovat 2-round partition (12 + 12 = 24)
- Source diversity heuristic (max 2 per source unit)
- Question Bank immutability & isolation (Question.round_id remains NULL)
- API endpoint authentication & authorization (anonymous 401, player 403, owner 200/201)
- Fail-closed publish safety (draft status enforced)
"""

import os
import uuid
import pytest
from fastapi import status
from sqlalchemy.orm import Session

from models import User, Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer
from services.pack_generator import (
    normalize_question_text,
    is_question_eligible,
    get_question_source_unit,
    generate_candidate_pack,
    commit_generated_pack,
)
from api.drafts import run_publish_validation


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================

@pytest.fixture
def owner_email_env(monkeypatch):
    """Configures a unique dynamic test owner email."""
    dynamic_owner = f"test_owner_{uuid.uuid4().hex[:8]}@example.test"
    monkeypatch.setenv("OWNER_EMAIL", dynamic_owner)
    return dynamic_owner


def register_and_login_user(client, email: str, display_name: str = "Test User") -> dict:
    """Helper to register and authenticate a user in the test client session."""
    client.cookies.clear()
    res = client.post("/api/auth/register", json={
        "email": email,
        "password": "ValidPassword123!",
        "display_name": display_name,
    })
    assert res.status_code == status.HTTP_201_CREATED, res.text
    return res.json()["user"]


def seed_test_bank(db_session: Session, count: int = 30) -> list[Question]:
    """Seeds clean, eligible Question Bank questions."""
    questions = []
    for i in range(1, count + 1):
        q = Question(
            text=f"Zakovat savoli #{i}: Dunyo poytaxtlari haqida savol {i}?",
            category="Geografiya" if i % 2 == 0 else "Tarix",
            status="approved",
            question_type="text",
            default_points=1,
            points=1,
            round_id=None,
            source_meta={
                "source_name": f"source_pack_{((i - 1) // 5) + 1}",
                "question_message_id": ((i - 1) // 2) + 100,  # 2 questions per message unit
                "editorial": {"decision": "approved"},
            }
        )
        db_session.add(q)
        db_session.flush()

        ans_prim = AcceptedAnswer(question_id=q.id, answer_text=f"Poytaxt {i}", is_primary=True)
        ans_alt = AcceptedAnswer(question_id=q.id, answer_text=f"Shahar {i}", is_primary=False)
        db_session.add_all([ans_prim, ans_alt])
        questions.append(q)

    db_session.commit()
    return questions


# =============================================================================
# 1. TEXT NORMALIZATION & DEDUPLICATION
# =============================================================================

def test_normalize_question_text():
    """Verifies lowercase, punctuation stripping, whitespace collapsing, and Uzbek apostrophes."""
    # Empty / None
    assert normalize_question_text("") == ""
    assert normalize_question_text(None) == ""

    # Uzbek apostrophe variants: ' (single quote), ʻ (modifier letter turned comma), ’ (right single quote), ‘ (left single quote)
    t1 = "O'zbekiston — buyuk davlat!"
    t2 = "Oʻzbekiston   buyuk davlat?"
    t3 = "O’zbekiston buyuk davlat."
    t4 = "O‘zbekiston, buyuk davlat;"

    norm1 = normalize_question_text(t1)
    norm2 = normalize_question_text(t2)
    norm3 = normalize_question_text(t3)
    norm4 = normalize_question_text(t4)

    assert norm1 == norm2 == norm3 == norm4
    assert norm1 == "o'zbekiston buyuk davlat"


# =============================================================================
# 2. AUTHORITATIVE ELIGIBILITY FILTERING
# =============================================================================

def test_eligibility_valid_question(db_session):
    """Clean text question with primary answer is eligible."""
    q = Question(
        text="Normal Zakovat savoli matni",
        status="ready",
        question_type="text",
    )
    db_session.add(q)
    db_session.flush()
    ans = AcceptedAnswer(question_id=q.id, answer_text="Alisher Navoiy", is_primary=True)
    db_session.add(ans)
    db_session.commit()

    ok, reason = is_question_eligible(q)
    assert ok is True
    assert reason == ""


def test_eligibility_empty_text(db_session):
    """Empty or whitespace-only question text is ineligible."""
    q = Question(text="   ", status="ready", question_type="text")
    ok, reason = is_question_eligible(q)
    assert ok is False
    assert "bo'sh" in reason.lower()


def test_eligibility_incompatible_type(db_session):
    """Multiple choice or non-text questions are ineligible for Classic Zakovat."""
    q = Question(text="Savol matni", status="ready", question_type="multiple_choice")
    ok, reason = is_question_eligible(q)
    assert ok is False
    assert "mos kelmaydigan savol turi" in reason.lower()


def test_eligibility_missing_primary_answer(db_session):
    """Question without primary answer is ineligible."""
    q = Question(text="Savol matni", status="ready", question_type="text")
    db_session.add(q)
    db_session.flush()
    # Only alternative answer
    ans = AcceptedAnswer(question_id=q.id, answer_text="Alt faqat", is_primary=False)
    db_session.add(ans)
    db_session.commit()

    ok, reason = is_question_eligible(q)
    assert ok is False
    assert "primary answer" in reason.lower()


def test_eligibility_dirty_answers(db_session):
    """Primary answer with parentheses or quotation marks is ineligible."""
    # Quotes
    q1 = Question(text="Savol 1", status="ready", question_type="text")
    db_session.add(q1)
    db_session.flush()
    ans1 = AcceptedAnswer(question_id=q1.id, answer_text='“O\'tkan kunlar”', is_primary=True)
    db_session.add(ans1)

    # Parentheses
    q2 = Question(text="Savol 2", status="ready", question_type="text")
    db_session.add(q2)
    db_session.flush()
    ans2 = AcceptedAnswer(question_id=q2.id, answer_text='Samarqand (Registon)', is_primary=True)
    db_session.add(ans2)
    db_session.commit()

    ok1, reason1 = is_question_eligible(q1)
    assert ok1 is False
    assert "qo'shtirnoq" in reason1.lower()

    ok2, reason2 = is_question_eligible(q2)
    assert ok2 is False
    assert "qavslar" in reason2.lower()


def test_eligibility_media_dependency(db_session):
    """Handout / visual references in text are ineligible."""
    q = Question(
        text="Ekranda ko'rib turganingizdek, ushbu fotosuratda qaysi bino tasvirlangan?",
        status="ready",
        question_type="text",
    )
    db_session.add(q)
    db_session.flush()
    ans = AcceptedAnswer(question_id=q.id, answer_text="Eiffel", is_primary=True)
    db_session.add(ans)
    db_session.commit()

    ok, reason = is_question_eligible(q)
    assert ok is False
    assert "tarqatma" in reason.lower() or "rasm" in reason.lower()


def test_eligibility_editorial_decisions(db_session):
    """Rejected and needs_review questions are ineligible."""
    # Rejected status
    q_rej = Question(text="Savol matni", status="rejected", question_type="text")
    ok_r, _ = is_question_eligible(q_rej)
    assert ok_r is False

    # Needs review status
    q_rev = Question(text="Savol matni", status="needs_review", question_type="text")
    ok_v, _ = is_question_eligible(q_rev)
    assert ok_v is False

    # Source meta editorial rejected
    q_meta_rej = Question(
        text="Savol matni",
        status="ready",
        question_type="text",
        source_meta={"editorial": {"decision": "rejected"}}
    )
    ok_mr, _ = is_question_eligible(q_meta_rej)
    assert ok_mr is False


# =============================================================================
# 3. DETERMINISM & STRUCTURE
# =============================================================================

def test_generate_candidate_pack_deterministic(db_session):
    """Identical seed on identical question pool produces identical questions."""
    seed_test_bank(db_session, count=30)

    cand1 = generate_candidate_pack(db_session, seed=42)
    cand2 = generate_candidate_pack(db_session, seed=42)

    assert cand1["selected_question_ids"] == cand2["selected_question_ids"]
    assert len(cand1["selected_question_ids"]) == 24
    assert cand1["round_1_count"] == 12
    assert cand1["round_2_count"] == 12

    # Different seed produces different question selection / ordering
    cand3 = generate_candidate_pack(db_session, seed=999)
    assert cand1["selected_question_ids"] != cand3["selected_question_ids"]


def test_generate_candidate_pack_source_diversity(db_session):
    """Enforces max 2 questions per source unit."""
    seed_test_bank(db_session, count=30)

    cand = generate_candidate_pack(db_session, seed=777, max_per_source_unit=2)
    assert cand["source_diversity"]["max_per_source_unit"] <= 2
    assert cand["source_diversity"]["unique_source_units"] >= 12


def test_generate_candidate_pack_insufficient_pool(db_session):
    """Raises ValueError when fewer than 24 eligible questions exist."""
    # Seed only 10 questions
    seed_test_bank(db_session, count=10)

    with pytest.raises(ValueError, match="yetarli yaroqli savollar topilmadi"):
        generate_candidate_pack(db_session, seed=123)


# =============================================================================
# 4. QUESTION BANK IMMUTABILITY & DRAFT COMMIT
# =============================================================================

def test_commit_generated_pack_isolation(db_session):
    """
    Committing a generated pack:
    - Creates Quiz and QuizVersion (status='draft')
    - Creates Round 1 (12 questions) and Round 2 (12 questions)
    - Question Bank Question.round_id remains NULL
    - Questions are untouched
    """
    questions = seed_test_bank(db_session, count=28)
    cand = generate_candidate_pack(db_session, seed=555)

    quiz, version = commit_generated_pack(
        db_session,
        candidate_data=cand,
        owner_email="owner@test.uz",
        title="Seeded Test Draft Pack",
        description="Pack Generator v1 Verification",
    )

    assert quiz.id is not None
    assert quiz.title == "Seeded Test Draft Pack"
    assert version.id is not None
    assert version.status == "draft"
    assert version.version_number == 1

    # Verify rounds
    rounds = db_session.query(Round).filter(Round.quiz_version_id == version.id).order_by(Round.sequence).all()
    assert len(rounds) == 2
    assert rounds[0].round_type == "classic_round_1"
    assert rounds[1].round_type == "classic_round_2"

    # Verify round questions
    r1_rqs = db_session.query(RoundQuestion).filter(RoundQuestion.round_id == rounds[0].id).order_by(RoundQuestion.sequence).all()
    r2_rqs = db_session.query(RoundQuestion).filter(RoundQuestion.round_id == rounds[1].id).order_by(RoundQuestion.sequence).all()
    assert len(r1_rqs) == 12
    assert len(r2_rqs) == 12

    # CRITICAL: Verify Question Bank Question records are NEVER mutated
    for q in questions:
        db_session.refresh(q)
        assert q.round_id is None, f"Question #{q.id} round_id was mutated to {q.round_id}!"

    # Verify publish validation passes for the structure
    val = run_publish_validation(version, db_session)
    assert val["total_rounds"] == 2
    assert val["total_questions"] == 24
    assert len(val["errors"]) == 0


# =============================================================================
# 5. API AUTHORIZATION & ENDPOINTS
# =============================================================================

def test_api_generator_anonymous_rejected_401(client, owner_email_env):
    """Anonymous visitors receive 401 on pack generator endpoints."""
    client.cookies.clear()
    res1 = client.post("/api/owner/pack-generator/preview", json={"seed": 12345})
    assert res1.status_code == status.HTTP_401_UNAUTHORIZED

    res2 = client.post("/api/owner/pack-generator/generate", json={"seed": 12345})
    assert res2.status_code == status.HTTP_401_UNAUTHORIZED


def test_api_generator_normal_user_rejected_403(client, owner_email_env):
    """Non-owner authenticated users receive 403 Forbidden."""
    normal_email = f"normal_{uuid.uuid4().hex[:8]}@example.test"
    register_and_login_user(client, normal_email, "Normal Player")

    res1 = client.post("/api/owner/pack-generator/preview", json={"seed": 12345})
    assert res1.status_code == status.HTTP_403_FORBIDDEN

    res2 = client.post("/api/owner/pack-generator/generate", json={"seed": 12345})
    assert res2.status_code == status.HTTP_403_FORBIDDEN


def test_api_generator_owner_preview_and_generate(client, owner_email_env, db_session):
    """Owner can preview and generate a draft pack via API."""
    seed_test_bank(db_session, count=30)
    register_and_login_user(client, owner_email_env, "Platform Owner")

    # 1. Preview
    res_prev = client.post("/api/owner/pack-generator/preview", json={
        "seed": 88888,
        "game_mode": "classic_zakovat",
        "exclude_attached": True,
    })
    assert res_prev.status_code == status.HTTP_200_OK
    prev_data = res_prev.json()
    assert prev_data["seed"] == 88888
    assert prev_data["total_questions"] == 24
    assert len(prev_data["round_1_questions"]) == 12
    assert len(prev_data["round_2_questions"]) == 12
    assert prev_data["validation_summary"]["valid"] is True

    # 2. Generate (Commit)
    res_gen = client.post("/api/owner/pack-generator/generate", json={
        "seed": 88888,
        "game_mode": "classic_zakovat",
        "title": "API Generated Test Pack",
        "description": "API Test",
        "exclude_attached": True,
    })
    assert res_gen.status_code == status.HTTP_201_CREATED
    gen_data = res_gen.json()
    assert gen_data["success"] is True
    assert gen_data["status"] == "draft"
    assert gen_data["total_questions"] == 24
    quiz_id = gen_data["quiz_id"]

    # 3. Verify in owner quiz detail API
    res_detail = client.get(f"/api/owner/quizzes/{quiz_id}")
    assert res_detail.status_code == status.HTTP_200_OK
    detail_data = res_detail.json()
    assert detail_data["status"] == "draft"
    assert detail_data["total_questions"] == 24
    assert detail_data["total_rounds"] == 2
    assert len(detail_data["rounds"][0]["questions"]) == 12
    assert len(detail_data["rounds"][1]["questions"]) == 12
