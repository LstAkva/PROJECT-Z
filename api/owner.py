import os
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import func, distinct, or_, text, case

from database import get_db
from models import User, Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, DailySubmission
from api.auth import get_current_user_required
from api.drafts import run_publish_validation, execute_publish_version, CANONICAL_ROUND_TYPES
from services.editorial import (
    get_question_qa_info,
    get_question_editorial_status,
    record_editorial_decision,
    DOCUMENTED_REPLACEMENT_CANDIDATES,
)
from services.pack_generator import (
    generate_candidate_pack,
    commit_generated_pack,
)

router = APIRouter(prefix="/api/owner", tags=["owner"])


# =============================================================================
# 1. OWNER AUTHORIZATION LAYER
# =============================================================================

from api.auth import get_owner_emails, verify_owner_access


# =============================================================================
# 2. SCHEMAS
# =============================================================================

class CreateQuizRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = None
    game_mode: str = Field(default="classic_zakovat")


class UpdateQuizRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = None


class AttachQuestionRequest(BaseModel):
    question_id: int
    sequence: Optional[int] = None


class ReorderQuestionsRequest(BaseModel):
    ordered_rq_ids: List[int]


class ReplaceQuestionRequest(BaseModel):
    new_question_id: int


class PublishQuizRequest(BaseModel):
    visibility: str = Field(default="public")
    category: Optional[str] = None


class PackGeneratorPreviewRequest(BaseModel):
    seed: int
    game_mode: str = Field(default="classic_zakovat")
    exclude_attached: bool = Field(default=True)


class PackGeneratorGenerateRequest(BaseModel):
    seed: int
    game_mode: str = Field(default="classic_zakovat")
    title: Optional[str] = None
    description: Optional[str] = None
    exclude_attached: bool = Field(default=True)


class EditorialDecisionRequest(BaseModel):
    decision: str = Field(..., description="approve, reject, or needs_review")
    notes: Optional[str] = None


class BatchEditorialDecisionRequest(BaseModel):
    decision: str = Field(..., description="approve, reject, or needs_review")
    question_ids: Optional[List[int]] = None
    notes: Optional[str] = None


class AddAcceptedAnswerRequest(BaseModel):
    answer_text: str = Field(..., min_length=1, max_length=255)
    is_primary: bool = Field(default=False)


class UpdateOwnerUserRequest(BaseModel):
    display_name: str = Field(..., min_length=2, max_length=100)



# =============================================================================
# 3. ENDPOINTS
# =============================================================================

@router.get("/status", status_code=status.HTTP_200_OK)
def get_owner_system_status(
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Returns owner-only operational and safety telemetry.
    CRITICAL: Never exposes passwords, raw connection strings, or secrets.
    """
    raw_url = os.getenv("DATABASE_URL", "")
    db_host = "unknown"
    if raw_url:
        try:
            parsed = urllib.parse.urlparse(raw_url)
            db_host = parsed.hostname or "unknown"
        except Exception:
            db_host = "unknown"

    # Alembic revision: fetch from alembic_version table if present, else known head
    alembic_head = "9c0d1e2f3a4b"
    try:
        row = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
        if row:
            alembic_head = row[0]
    except Exception:
        pass

    draft_count = db.query(QuizVersion).filter(QuizVersion.status == "draft").count()
    published_count = db.query(QuizVersion).filter(QuizVersion.status == "published").count()
    archived_count = db.query(QuizVersion).filter(QuizVersion.status == "archived").count()
    bank_count = db.query(Question).filter(Question.status.in_(["ready", "needs_review"])).count()
    used_questions_count = db.query(distinct(RoundQuestion.question_id)).count()

    return {
        "status": "online",
        "environment": os.getenv("ENVIRONMENT", "development"),
        "db_target": os.getenv("DB_TARGET", "development"),
        "db_host": db_host,
        "alembic_revision": alembic_head,
        "db_safety_guard_active": True,
        "owner_email": current_owner.email,
        "metrics": {
            "draft_quizzes": draft_count,
            "published_quizzes": published_count,
            "archived_quizzes": archived_count,
            "question_bank_total": bank_count,
            "questions_attached_to_packs": used_questions_count,
        },
    }


@router.get("/daily-bank", status_code=status.HTTP_200_OK)
def get_daily_bank_status(
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """Read-only: how many Daily Questions the bank can still serve, counted with the scheduler's own
    pool, eligibility rule and uniqueness key. Never schedules, marks or edits anything."""
    from services.daily_question import get_verified_pool, is_candidate_eligible, get_tashkent_today
    from services.daily_engagement import compute_bank_status
    return compute_bank_status(db, get_verified_pool(), get_tashkent_today(), is_candidate_eligible)


@router.get("/quizzes", status_code=status.HTTP_200_OK)
def list_owner_quizzes(
    status_filter: Optional[str] = Query("all", alias="status"),
    game_mode: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Lists quizzes with draft/published/archived status and metadata.
    """
    versions = (
        db.query(QuizVersion)
        .join(Quiz, QuizVersion.quiz_id == Quiz.id)
        .options(
            joinedload(QuizVersion.quiz),
            selectinload(QuizVersion.rounds).selectinload(Round.round_questions),
        )
        .order_by(Quiz.created_at.desc(), QuizVersion.version_number.desc())
        .all()
    )
    latest_by_quiz = {}
    for v in versions:
        if v.quiz_id not in latest_by_quiz:
            latest_by_quiz[v.quiz_id] = v

    results = []
    for latest_ver in latest_by_quiz.values():
        q = latest_ver.quiz
        if not q:
            continue

        if status_filter and status_filter.lower() != "all":
            if latest_ver.status != status_filter.lower():
                continue

        if game_mode and game_mode.strip():
            if latest_ver.game_mode != game_mode.strip():
                continue

        rounds = latest_ver.rounds
        total_questions = sum(len(r.round_questions) for r in rounds)

        # Validation summary
        if latest_ver.game_mode == "svoyak":
            is_valid = (latest_ver.status == "published") or (len(rounds) == 5 and total_questions == 25)
            val_errors_count = 0 if is_valid else (25 - total_questions if len(rounds) == 5 else 5)
        else:
            is_valid = (latest_ver.status == "published") or (len(rounds) == 2 and total_questions == 24)
            val_errors_count = 0 if is_valid else (24 - total_questions if len(rounds) == 2 else 2)

        results.append({
            "quiz_id": q.id,
            "version_id": latest_ver.id,
            "title": q.title,
            "description": q.description,
            "game_mode": latest_ver.game_mode,
            "status": latest_ver.status,
            "version_number": latest_ver.version_number,
            "round_count": len(rounds),
            "question_count": total_questions,
            "created_at": q.created_at.isoformat() if q.created_at else None,
            "published_at": latest_ver.published_at.isoformat() if latest_ver.published_at else None,
            "is_valid": is_valid,
            "validation_errors_count": val_errors_count,
            "validation_warnings_count": 0,
        })

    return results


@router.post("/quizzes", status_code=status.HTTP_201_CREATED)
def create_owner_quiz(
    payload: CreateQuizRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Creates a new draft quiz pack.
    For classic_zakovat: automatically initializes 1-tur and 2-tur as canonical rounds.
    """
    quiz = Quiz(
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
    )
    db.add(quiz)
    db.flush()

    mode = payload.game_mode or "classic_zakovat"
    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        status="draft",
        game_mode=mode,
        published_manifest=None,
    )
    db.add(version)
    db.flush()

    if mode == "classic_zakovat":
        r1 = Round(
            quiz_version_id=version.id,
            sequence=1,
            round_type="zakovat_classic",
            config={"name": "1-tur"},
        )
        r2 = Round(
            quiz_version_id=version.id,
            sequence=2,
            round_type="zakovat_classic",
            config={"name": "2-tur"},
        )
        db.add_all([r1, r2])

    db.commit()
    db.refresh(quiz)
    db.refresh(version)

    return {
        "quiz_id": quiz.id,
        "version_id": version.id,
        "title": quiz.title,
        "description": quiz.description,
        "game_mode": version.game_mode,
        "status": version.status,
        "version_number": version.version_number,
    }


@router.get("/quizzes/{quiz_id}", status_code=status.HTTP_200_OK)
def get_owner_quiz_detail(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Detailed owner view of a quiz and its latest version.
    Includes rounds, questions with continuous global numbering (#1..#24), and validation state.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id)
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina versiyasi topilmadi")

    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )

    rounds_data = []
    global_seq = 1

    for r in rounds:
        rqs = (
            db.query(RoundQuestion)
            .filter(RoundQuestion.round_id == r.id)
            .order_by(RoundQuestion.sequence.asc())
            .all()
        )
        questions_list = []
        for rq in rqs:
            q = rq.question
            primary_ans = next((a.answer_text for a in q.accepted_answers if a.is_primary), None)
            alt_answers = [a.answer_text for a in q.accepted_answers if not a.is_primary]
            all_answers = [a.answer_text for a in q.accepted_answers]

            qa_info = get_question_qa_info(q, db=db)
            ed_status = get_question_editorial_status(q)

            questions_list.append({
                "round_question_id": rq.id,
                "question_id": q.id,
                "round_sequence": rq.sequence,
                "global_sequence": global_seq,
                "text": q.text,
                "question_type": q.question_type,
                "category": q.category,
                "status": q.status,
                "editorial_status": ed_status,
                "qa_info": qa_info,
                "candidate_replacements": qa_info.get("candidate_replacements", []),
                "points": rq.points_override if rq.points_override is not None else (q.default_points or q.points or 1),
                "primary_answer": primary_ans,
                "alternative_answers": alt_answers,
                "all_answers": all_answers,
                "explanation": q.explanation,
                "source_meta": q.source_meta,
            })
            global_seq += 1

        r_title = (r.config or {}).get("name") or f"{r.sequence}-tur"
        rounds_data.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "round_title": r_title,
            "question_count": len(questions_list),
            "questions": questions_list,
        })

    val = run_publish_validation(version, db)

    return {
        "quiz_id": quiz.id,
        "version_id": version.id,
        "title": quiz.title,
        "description": quiz.description,
        "game_mode": version.game_mode,
        "status": version.status,
        "readiness_state": val.get("readiness_state", "Draft"),
        "is_ready_to_publish": val.get("is_ready_to_publish", False),
        "editorial_summary": val.get("editorial_summary", {}),
        "version_number": version.version_number,
        "created_at": quiz.created_at.isoformat() if quiz.created_at else None,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "total_rounds": len(rounds_data),
        "total_questions": global_seq - 1,
        "rounds": rounds_data,
        "validation": val,
    }


@router.put("/quizzes/{quiz_id}", status_code=status.HTTP_200_OK)
def update_owner_quiz(
    quiz_id: int,
    payload: UpdateQuizRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Updates draft quiz title and description.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id)
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if version and version.status == "published":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nashr qilingan viktorina metadata ma'lumotlarini bevosita o'zgartirib bo'lmaydi.",
        )

    if payload.title is not None:
        quiz.title = payload.title.strip()
    if payload.description is not None:
        quiz.description = payload.description.strip() if payload.description else None

    db.commit()
    db.refresh(quiz)

    return {
        "quiz_id": quiz.id,
        "title": quiz.title,
        "description": quiz.description,
    }


@router.delete("/quizzes/{quiz_id}", status_code=status.HTTP_200_OK)
def delete_owner_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Destructive delete for draft quizzes that have NEVER been published.
    SAFETY RULE: If a quiz has ever been published, destructive deletion is strictly forbidden.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    # Verify quiz has never been published
    published_versions = (
        db.query(QuizVersion)
        .filter(
            QuizVersion.quiz_id == quiz.id,
            or_(QuizVersion.status == "published", QuizVersion.published_at.isnot(None)),
        )
        .count()
    )
    if published_versions > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ushbu viktorina avval nashr qilingan. Tarixiy ma'lumotlarni saqlash maqsadida "
                   "uni butunlay o'chirib bo'lmaydi. Arxivlash funksiyasidan foydalaning.",
        )

    # Safe delete of draft container and RoundQuestion links (Question Bank remains intact)
    db.delete(quiz)
    db.commit()

    return {"success": True, "message": "Qoralama viktorina o'chirildi"}


@router.post("/quizzes/{quiz_id}/archive", status_code=status.HTTP_200_OK)
def archive_owner_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Archives a quiz (sets status='archived') safely preserving historical records.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id)
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina versiyasi topilmadi")

    version.status = "archived"
    db.commit()

    return {"success": True, "message": "Viktorina arxivlandi"}


# =============================================================================
# 4. QUESTION BANK OWNER ENDPOINTS
# =============================================================================

@router.get("/questions", status_code=status.HTTP_200_OK)
def list_owner_question_bank(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    round_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    exclude_quiz_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Owner-only Question Bank explorer.
    Supports text search, answer search, category/round_type filters, and optional quiz-scoped exclusion.
    """
    query = db.query(Question)
    valid_bank_statuses = ["ready", "needs_review", "approved", "rejected", "draft"]
    if status_filter and status_filter.lower() != "all":
        query = query.filter(Question.status == status_filter.lower())
    else:
        query = query.filter(Question.status.in_(["ready", "needs_review", "approved"]))

    if category and category.lower() != "all":
        query = query.filter(Question.category == category)

    if round_type and round_type.lower() != "all":
        try:
            query = query.filter(
                or_(
                    Question.compound_type == round_type,
                    Question.source_meta["round_type"].astext == round_type,
                )
            )
        except Exception:
            pass

    if search and search.strip():
        term = f"%{search.strip()}%"
        # Search question text OR accepted answer text
        matching_q_ids = (
            db.query(distinct(AcceptedAnswer.question_id))
            .filter(AcceptedAnswer.answer_text.ilike(term))
            .all()
        )
        answer_q_ids = [qid[0] for qid in matching_q_ids]
        query = query.filter(or_(Question.text.ilike(term), Question.id.in_(answer_q_ids)))

    if exclude_quiz_id:
        attached_ids = (
            db.query(distinct(RoundQuestion.question_id))
            .join(Round, RoundQuestion.round_id == Round.id)
            .join(QuizVersion, Round.quiz_version_id == QuizVersion.id)
            .filter(QuizVersion.quiz_id == exclude_quiz_id)
            .all()
        )
        excluded = [qid[0] for qid in attached_ids]
        if excluded:
            query = query.filter(Question.id.notin_(excluded))

    total = query.count()
    items = (
        query.options(selectinload(Question.accepted_answers))
        .order_by(Question.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    item_ids = [q.id for q in items]
    used_counts = dict(
        db.query(RoundQuestion.question_id, func.count(RoundQuestion.id))
        .filter(RoundQuestion.question_id.in_(item_ids))
        .group_by(RoundQuestion.question_id)
        .all()
    ) if item_ids else {}

    formatted_items = []
    for q in items:
        primary_ans = next((a.answer_text for a in q.accepted_answers if a.is_primary), None)
        alt_answers = [a.answer_text for a in q.accepted_answers if not a.is_primary]
        all_answers = [a.answer_text for a in q.accepted_answers]

        used_count = used_counts.get(q.id, 0)
        qa_info = get_question_qa_info(q)
        ed_status = get_question_editorial_status(q)

        formatted_items.append({
            "id": q.id,
            "text": q.text,
            "question_type": q.question_type,
            "category": q.category,
            "status": q.status,
            "editorial_status": ed_status,
            "qa_status": qa_info.get("audit_status", "PASS"),
            "points": q.default_points or q.points or 1,
            "primary_answer": primary_ans,
            "alternative_answers": alt_answers,
            "all_answers": all_answers,
            "source_meta": q.source_meta,
            "times_used_in_packs": used_count,
        })

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": formatted_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/questions/{question_id}", status_code=status.HTTP_200_OK)
def get_owner_question_detail(
    question_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Full question detail including primary/alternative answers, provenance, QA info, candidates, and pack usage.
    """
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol topilmadi")

    primary_ans = next((a.answer_text for a in q.accepted_answers if a.is_primary), None)
    alt_answers = [a.answer_text for a in q.accepted_answers if not a.is_primary]
    all_answers = [a.answer_text for a in q.accepted_answers]
    structured_answers = [
        {"id": a.id, "answer_text": a.answer_text, "is_primary": a.is_primary}
        for a in q.accepted_answers
    ]

    qa_info = get_question_qa_info(q, db=db)
    ed_status = get_question_editorial_status(q)
    editorial = (q.source_meta or {}).get("editorial", {}) if isinstance(q.source_meta, dict) else {}

    # Find quizzes using this question
    rqs = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.question_id == q.id)
        .all()
    )
    used_in = []
    for rq in rqs:
        r = rq.round
        qv = r.quiz_version if r else None
        quiz = qv.quiz if qv else None
        if quiz:
            used_in.append({
                "quiz_id": quiz.id,
                "quiz_title": quiz.title,
                "round_sequence": r.sequence,
                "question_sequence": rq.sequence,
                "version_status": qv.status,
            })

    return {
        "id": q.id,
        "text": q.text,
        "question_type": q.question_type,
        "category": q.category,
        "status": q.status,
        "editorial_status": ed_status,
        "editorial": editorial,
        "qa_info": qa_info,
        "candidate_replacements": qa_info.get("candidate_replacements", []),
        "points": q.default_points or q.points or 1,
        "explanation": q.explanation,
        "options": q.options,
        "primary_answer": primary_ans,
        "alternative_answers": alt_answers,
        "all_answers": all_answers,
        "accepted_answers": structured_answers,
        "source_meta": q.source_meta,
        "used_in_quizzes": used_in,
    }


@router.post("/questions/{question_id}/decision", status_code=status.HTTP_200_OK)
def set_question_editorial_decision(
    question_id: int,
    payload: EditorialDecisionRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Sets owner editorial decision ('approve', 'reject', 'needs_review') on a Question.
    Updates Question.status and records decision metadata in Question.source_meta['editorial'].
    """
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol topilmadi")

    try:
        new_status = record_editorial_decision(
            question=q,
            decision=payload.decision,
            owner_email=current_owner.email,
            notes=payload.notes,
            actor_type="authenticated_owner",
            executed_by=current_owner.email,
            authorizing_authority=current_owner.email,
            action_type="owner_single_decision",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    db.commit()
    db.refresh(q)

    return {
        "success": True,
        "question_id": q.id,
        "editorial_status": new_status,
        "status": q.status,
        "editorial": q.source_meta.get("editorial", {}),
    }


@router.post("/quizzes/{quiz_id}/decision", status_code=status.HTTP_200_OK)
def set_quiz_batch_editorial_decision(
    quiz_id: int,
    payload: BatchEditorialDecisionRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Batch applies an editorial decision ('approve', 'reject', 'needs_review') to questions in a quiz.
    If payload.question_ids is specified, only applies to those question IDs.
    Otherwise, applies to all questions attached to the quiz draft.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qoralama topilmadi")

    attached_qs = (
        db.query(Question)
        .join(RoundQuestion, RoundQuestion.question_id == Question.id)
        .join(Round, RoundQuestion.round_id == Round.id)
        .filter(Round.quiz_version_id == version.id)
        .all()
    )

    if payload.question_ids:
        target_set = set(payload.question_ids)
        target_qs = [q for q in attached_qs if q.id in target_set]
    else:
        target_qs = attached_qs

    updated_ids = []
    for q in target_qs:
        record_editorial_decision(
            question=q,
            decision=payload.decision,
            owner_email=current_owner.email,
            notes=payload.notes,
            actor_type="authenticated_owner",
            executed_by=current_owner.email,
            authorizing_authority=current_owner.email,
            action_type="owner_batch_decision",
        )
        updated_ids.append(q.id)

    db.commit()

    val = run_publish_validation(version, db)

    return {
        "success": True,
        "quiz_id": quiz.id,
        "applied_decision": payload.decision,
        "updated_question_count": len(updated_ids),
        "updated_question_ids": updated_ids,
        "readiness_state": val.get("readiness_state"),
        "is_ready_to_publish": val.get("is_ready_to_publish"),
        "editorial_summary": val.get("editorial_summary"),
        "validation_valid": val.get("valid"),
    }


@router.post("/questions/{question_id}/answers", status_code=status.HTTP_201_CREATED)
def add_question_accepted_answer(
    question_id: int,
    payload: AddAcceptedAnswerRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Adds a new accepted answer or synonym for a Question.
    If is_primary=True, demotes any other answers to non-primary.
    """
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol topilmadi")

    ans_clean = payload.answer_text.strip()
    if not ans_clean:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Javob matni bo'sh bo'lishi mumkin emas")

    existing = (
        db.query(AcceptedAnswer)
        .filter(AcceptedAnswer.question_id == q.id, func.lower(AcceptedAnswer.answer_text) == func.lower(ans_clean))
        .first()
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bu javob varianti allaqachon mavjud")

    if payload.is_primary:
        for a in q.accepted_answers:
            if a.is_primary:
                a.is_primary = False

    new_ans = AcceptedAnswer(
        question_id=q.id,
        answer_text=ans_clean,
        is_primary=payload.is_primary,
    )
    db.add(new_ans)
    db.commit()
    db.refresh(new_ans)

    return {
        "success": True,
        "answer_id": new_ans.id,
        "question_id": q.id,
        "answer_text": new_ans.answer_text,
        "is_primary": new_ans.is_primary,
    }


@router.delete("/questions/{question_id}/answers/{answer_id}", status_code=status.HTTP_200_OK)
def delete_question_accepted_answer(
    question_id: int,
    answer_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Deletes an accepted answer.
    Guards: Cannot delete the only remaining answer. If primary is deleted, another answer is designated primary.
    """
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol topilmadi")

    ans = (
        db.query(AcceptedAnswer)
        .filter(AcceptedAnswer.id == answer_id, AcceptedAnswer.question_id == q.id)
        .first()
    )
    if not ans:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Javob topilmadi")

    all_answers = list(q.accepted_answers)
    if len(all_answers) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Savolda kamida bitta to'g'ri javob qolishi shart. Yagona javobni o'chirib bo'lmaydi.",
        )

    was_primary = ans.is_primary
    db.delete(ans)
    db.flush()

    if was_primary:
        remaining = [a for a in q.accepted_answers if a.id != ans.id]
        if remaining:
            remaining[0].is_primary = True

    db.commit()

    return {"success": True, "message": "Javob varianti o'chirildi"}


@router.put("/questions/{question_id}/answers/{answer_id}/primary", status_code=status.HTTP_200_OK)
def set_question_primary_answer(
    question_id: int,
    answer_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Designates target answer as the primary answer, demoting all other answers for this question.
    """
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol topilmadi")

    ans = (
        db.query(AcceptedAnswer)
        .filter(AcceptedAnswer.id == answer_id, AcceptedAnswer.question_id == q.id)
        .first()
    )
    if not ans:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Javob topilmadi")

    for a in q.accepted_answers:
        a.is_primary = (a.id == ans.id)

    db.commit()

    return {
        "success": True,
        "primary_answer_id": ans.id,
        "primary_answer_text": ans.answer_text,
    }



# =============================================================================
# 5. PACK CONTENT OPERATIONS (RoundQuestion Management)
# =============================================================================

@router.post("/quizzes/{quiz_id}/rounds/{round_id}/attach-question", status_code=status.HTTP_201_CREATED)
def attach_question_to_round(
    quiz_id: int,
    round_id: int,
    payload: AttachQuestionRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Attaches a question from Question Bank to a round as RoundQuestion.
    CRITICAL: Leaves Question Bank Question completely untouched (Question.round_id remains NULL).
    Guards against duplicate questions within the same quiz.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faqat qoralama (draft) holatidagi viktorinaga savol qo'shish mumkin")

    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi yoki ushbu viktorinaga tegishli emas")

    question = db.query(Question).filter(Question.id == payload.question_id).first()
    if not question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tanlangan savol Question Bankda topilmadi")

    # Duplicate check within this quiz
    existing_in_quiz = (
        db.query(RoundQuestion)
        .join(Round, RoundQuestion.round_id == Round.id)
        .filter(
            Round.quiz_version_id == version.id,
            RoundQuestion.question_id == question.id,
        )
        .first()
    )
    if existing_in_quiz:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ushbu savol (#{question.id}) mazkur viktorinaga allaqachon biriktirilgan (Tur #{existing_in_quiz.round.sequence}, Savol #{existing_in_quiz.sequence}).",
        )

    # Next sequence
    max_seq = (
        db.query(func.max(RoundQuestion.sequence))
        .filter(RoundQuestion.round_id == round_obj.id)
        .scalar()
        or 0
    )
    target_seq = payload.sequence if payload.sequence is not None else (max_seq + 1)

    rq = RoundQuestion(
        round_id=round_obj.id,
        question_id=question.id,
        sequence=target_seq,
        points_override=None,
    )
    db.add(rq)
    db.commit()
    db.refresh(rq)

    return {
        "round_question_id": rq.id,
        "round_id": round_obj.id,
        "question_id": question.id,
        "sequence": rq.sequence,
        "text": question.text,
    }


@router.delete("/quizzes/{quiz_id}/rounds/{round_id}/questions/{rq_id}", status_code=status.HTTP_200_OK)
def remove_question_from_round(
    quiz_id: int,
    round_id: int,
    rq_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Removes RoundQuestion link from round.
    CRITICAL: Question in Question Bank remains untouched.
    Re-indexes remaining questions in that round contiguously 1..N.
    """
    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faqat qoralamadan savol o'chirish mumkin")

    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    rq = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.id == rq_id, RoundQuestion.round_id == round_obj.id)
        .first()
    )
    if not rq:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol raundda topilmadi")

    db.delete(rq)
    db.flush()

    # Re-index remaining questions contiguously
    remaining = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.round_id == round_obj.id)
        .order_by(RoundQuestion.sequence.asc())
        .all()
    )
    for idx, item in enumerate(remaining, start=1):
        item.sequence = idx

    db.commit()

    return {"success": True, "message": "Savol raunddan olib tashlandi va qolganlar qayta raqamlandi"}


@router.put("/quizzes/{quiz_id}/rounds/{round_id}/questions/reorder", status_code=status.HTTP_200_OK)
def reorder_round_questions(
    quiz_id: int,
    round_id: int,
    payload: ReorderQuestionsRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Reorders questions within a round.
    Validates that all provided IDs belong to this round, with no duplicates or omissions.
    """
    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faqat qoralama tahrirlanadi")

    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    existing_rqs = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.round_id == round_obj.id)
        .all()
    )
    existing_map = {rq.id: rq for rq in existing_rqs}

    if set(payload.ordered_rq_ids) != set(existing_map.keys()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Taqdim etilgan savol ID lari raunddagi savollar ro'yxatiga to'liq mos kelmadi",
        )

    # Use a temporary negative offset to avoid unique constraint collisions during swap
    for idx, rq_id in enumerate(payload.ordered_rq_ids, start=1):
        existing_map[rq_id].sequence = -idx
    db.flush()

    for idx, rq_id in enumerate(payload.ordered_rq_ids, start=1):
        existing_map[rq_id].sequence = idx

    db.commit()

    return {"success": True, "message": "Savollar tartibi muvaffaqiyatli saqlandi"}


@router.post("/quizzes/{quiz_id}/rounds/{round_id}/questions/{rq_id}/replace", status_code=status.HTTP_200_OK)
def replace_question_in_round(
    quiz_id: int,
    round_id: int,
    rq_id: int,
    payload: ReplaceQuestionRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Replaces target RoundQuestion.question_id with new_question_id from Question Bank.
    Question Bank remains untouched.
    Guards against duplicate questions within the same quiz.
    """
    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faqat qoralama tahrirlanadi")

    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    rq = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.id == rq_id, RoundQuestion.round_id == round_obj.id)
        .first()
    )
    if not rq:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol raundda topilmadi")

    new_q = db.query(Question).filter(Question.id == payload.new_question_id).first()
    if not new_q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Yangi savol Question Bankda topilmadi")

    # Check duplicate in same quiz (excluding this rq itself)
    existing_in_quiz = (
        db.query(RoundQuestion)
        .join(Round, RoundQuestion.round_id == Round.id)
        .filter(
            Round.quiz_version_id == version.id,
            RoundQuestion.question_id == new_q.id,
            RoundQuestion.id != rq.id,
        )
        .first()
    )
    if existing_in_quiz:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Yangi savol (#{new_q.id}) mazkur viktorinada allaqachon mavjud.",
        )

    rq.question_id = new_q.id
    db.commit()
    db.refresh(rq)

    return {
        "success": True,
        "round_question_id": rq.id,
        "new_question_id": new_q.id,
        "sequence": rq.sequence,
        "text": new_q.text,
    }


# =============================================================================
# 6. VALIDATION & PUBLISHING (Reusing Authoritative Services)
# =============================================================================

@router.post("/quizzes/{quiz_id}/validate", status_code=status.HTTP_200_OK)
def validate_owner_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Reuses authoritative run_publish_validation to validate quiz completeness.
    Enforces exact 2 rounds × 12 questions for classic Zakovat.
    Prefers the latest draft version when one exists; otherwise validates the latest published version read-only.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    # 1. Prefer latest draft version when one exists
    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    # 2. Otherwise, for a published quiz, validate the latest published version read-only
    if not version:
        version = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
            .order_by(QuizVersion.version_number.desc())
            .first()
        )

    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tekshirish uchun versiya topilmadi")

    return run_publish_validation(version, db)


@router.post("/quizzes/{quiz_id}/publish", status_code=status.HTTP_200_OK)
def publish_owner_quiz(
    quiz_id: int,
    payload: Optional[PublishQuizRequest] = None,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Authoritative publishing endpoint.
    Reuses execute_publish_version to validate, compile and freeze published_manifest,
    and make the quiz available in the public Arena.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "draft")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Qoralama versiya topilmadi")

    visibility = payload.visibility if payload else "public"
    category = payload.category if payload and payload.category else "Zakovat"

    return execute_publish_version(version, db, visibility=visibility, category=category)


@router.post("/quizzes/{quiz_id}/unpublish", status_code=status.HTTP_200_OK)
def unpublish_owner_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Unpublishes a published quiz pack, converting its status back to draft.
    Removes the pack from the public Arena catalog while keeping all content intact.
    """
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viktorina topilmadi")

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ushbu viktorinaning nashr etilgan versiyasi topilmadi",
        )

    version.status = "draft"
    version.published_manifest = None
    version.published_at = None
    db.commit()

    return {
        "success": True,
        "quiz_id": quiz.id,
        "version_id": version.id,
        "status": "draft",
        "message": f"'{quiz.title}' paketi nashrdan olindi va qoralama holatiga o'tkazildi.",
    }


# =============================================================================
# 7. PACK GENERATOR v1 (QUESTION BANK → DRAFT PACKS)
# =============================================================================

@router.post("/pack-generator/preview", status_code=status.HTTP_200_OK)
def preview_candidate_pack(
    payload: PackGeneratorPreviewRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    In-memory candidate pack preview.
    Generates a deterministic 24-question candidate pack without modifying the database.
    """
    try:
        candidate = generate_candidate_pack(
            db=db,
            seed=payload.seed,
            game_mode=payload.game_mode,
            exclude_attached=payload.exclude_attached,
        )
        return candidate
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/pack-generator/generate", status_code=status.HTTP_201_CREATED)
def generate_and_commit_pack(
    payload: PackGeneratorGenerateRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Generates and commits a candidate pack as a DRAFT quiz in the database.
    - Creates Quiz and QuizVersion (status='draft')
    - Creates Round 1 (12 questions) and Round 2 (12 questions)
    - Question Bank Question records remain untouched (round_id remains NULL)
    - Does NOT publish the pack.
    """
    try:
        candidate = generate_candidate_pack(
            db=db,
            seed=payload.seed,
            game_mode=payload.game_mode,
            exclude_attached=payload.exclude_attached,
        )
        quiz, version = commit_generated_pack(
            db=db,
            candidate_data=candidate,
            owner_email=current_owner.email,
            title=payload.title,
            description=payload.description,
        )
        return {
            "success": True,
            "quiz_id": quiz.id,
            "version_id": version.id,
            "status": version.status,
            "title": quiz.title,
            "seed": payload.seed,
            "total_questions": 24,
            "round_1_count": 12,
            "round_2_count": 12,
            "message": "Qoralama paket muvaffaqiyatli yaratildi. Endi tahririyat ko'rigidan o'tkazish mumkin.",
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# =============================================================================
# 8. USER MANAGEMENT
# =============================================================================

@router.get("/users", status_code=status.HTTP_200_OK)
def list_owner_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Paginated user directory for Owner.
    Supports searching by email and display_name.
    Includes aggregated stats for total attempts and completed attempts.
    """
    query = db.query(User)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(User.email.ilike(term), User.display_name.ilike(term)))

    total_users = query.count()
    users = (
        query.order_by(User.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    user_ids = [u.id for u in users]
    attempts_map = {}
    daily_map = {}

    if user_ids:
        # Aggregated solo attempt counts
        stats = (
            db.query(
                SoloAttempt.user_id,
                func.count(SoloAttempt.id).label("total"),
                func.sum(case((SoloAttempt.status == "completed", 1), else_=0)).label("completed"),
            )
            .filter(SoloAttempt.user_id.in_(user_ids))
            .group_by(SoloAttempt.user_id)
            .all()
        )
        for row in stats:
            attempts_map[row[0]] = {
                "total": int(row[1] or 0),
                "completed": int(row[2] or 0),
            }

        # Aggregated daily submissions count
        daily_stats = (
            db.query(
                DailySubmission.user_id,
                func.count(DailySubmission.id).label("total"),
            )
            .filter(DailySubmission.user_id.in_(user_ids))
            .group_by(DailySubmission.user_id)
            .all()
        )
        for row in daily_stats:
            daily_map[row[0]] = int(row[1] or 0)

    user_list = []
    for u in users:
        u_stats = attempts_map.get(u.id, {"total": 0, "completed": 0})
        user_list.append({
            "id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "auth_provider": u.auth_provider,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "total_attempts": u_stats["total"],
            "completed_attempts": u_stats["completed"],
            "daily_submissions_count": daily_map.get(u.id, 0),
        })

    return {
        "users": user_list,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_count": total_users,
            "total_pages": (total_users + per_page - 1) // per_page if total_users > 0 else 1,
        },
    }


@router.get("/users/{user_id}", status_code=status.HTTP_200_OK)
def get_owner_user_detail(
    user_id: int,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Fetches full user profile details, game history (SoloAttempt), and Daily Question submissions.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Foydalanuvchi topilmadi")

    # Fetch attempts
    attempts = (
        db.query(SoloAttempt)
        .filter(SoloAttempt.user_id == user.id)
        .options(joinedload(SoloAttempt.quiz_version).joinedload(QuizVersion.quiz))
        .order_by(SoloAttempt.started_at.desc())
        .limit(50)
        .all()
    )

    attempt_list = []
    for a in attempts:
        quiz_title = "Noma'lum"
        game_mode = "classic_zakovat"
        quiz_id = None
        if a.quiz_version:
            game_mode = a.quiz_version.game_mode
            if a.quiz_version.quiz:
                quiz_title = a.quiz_version.quiz.title
                quiz_id = a.quiz_version.quiz.id

        attempt_list.append({
            "id": a.id,
            "session_token": a.session_token,
            "quiz_id": quiz_id,
            "quiz_title": quiz_title,
            "game_mode": game_mode,
            "timer_mode": a.timer_mode,
            "status": a.status,
            "total_score": a.total_score,
            "total_correct": a.total_correct,
            "started_at": a.started_at.isoformat() if a.started_at else None,
            "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        })

    # Fetch daily submissions
    daily_subs = (
        db.query(DailySubmission)
        .filter(DailySubmission.user_id == user.id)
        .options(joinedload(DailySubmission.daily_question))
        .order_by(DailySubmission.answered_at.desc())
        .limit(50)
        .all()
    )

    daily_list = []
    for ds in daily_subs:
        schedule_date = ds.daily_question.schedule_date.isoformat() if ds.daily_question else None
        daily_list.append({
            "id": ds.id,
            "daily_question_id": ds.daily_question_id,
            "schedule_date": schedule_date,
            "submitted_text": ds.submitted_text,
            "is_correct": ds.is_correct,
            "answered_at": ds.answered_at.isoformat() if ds.answered_at else None,
        })

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "stats": {
            "total_attempts": len(attempts),
            "completed_attempts": sum(1 for a in attempts if a.status == "completed"),
            "daily_submissions": len(daily_subs),
        },
        "attempts": attempt_list,
        "daily_submissions": daily_list,
    }


@router.put("/users/{user_id}", status_code=status.HTTP_200_OK)
def update_owner_user(
    user_id: int,
    payload: UpdateOwnerUserRequest,
    db: Session = Depends(get_db),
    current_owner: User = Depends(verify_owner_access),
):
    """
    Safe account action to update a user's display name.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Foydalanuvchi topilmadi")

    new_name = payload.display_name.strip()
    if len(new_name) < 2 or len(new_name) > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Foydalanuvchi ismi 2 dan 100 tagacha belgidan iborat bo'lishi kerak",
        )

    user.display_name = new_name
    db.commit()
    db.refresh(user)

    return {
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "message": f"Foydalanuvchi ismi muvaffaqiyatli yangilandi: '{new_name}'",
    }

