from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt
from api.quizzes import CANONICAL_ROUND_TYPES, compile_published_manifest, sanitize_round_config

router = APIRouter(prefix="/api/drafts", tags=["drafts"])


# =========================================================
# PYDANTIC SCHEMAS
# =========================================================

class CreateDraftQuizRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    game_mode: Optional[str] = "modern_multiround"


class UpdateDraftQuizRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    game_mode: Optional[str] = None


class AddRoundRequest(BaseModel):
    round_type: str
    config: Optional[Dict[str, Any]] = None


class UpdateRoundRequest(BaseModel):
    round_type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class ReorderRoundsRequest(BaseModel):
    round_ids: List[int]


class ReorderQuestionsRequest(BaseModel):
    round_question_ids: List[int]


class AuthoredQuestionOption(BaseModel):
    key: str
    text: str


class AuthoredQuestionCreateRequest(BaseModel):
    text: str = Field(..., min_length=1)
    points: Optional[int] = 1
    explanation: Optional[str] = None
    media_url: Optional[str] = None
    media_provider: Optional[str] = None
    # For MCQ (gulmisiz_rayhonmisiz):
    options: Optional[List[AuthoredQuestionOption]] = None
    correct_option: Optional[str] = None  # 'A', 'B', 'C', 'D'
    # For text answers:
    primary_answer: Optional[str] = None
    accepted_answers: Optional[List[str]] = None
    # For boolean (aldama_meni):
    correct_boolean: Optional[bool] = None
    # Overrides / round-specific config:
    config_override: Optional[Dict[str, Any]] = None


class AuthoredQuestionUpdateRequest(BaseModel):
    text: Optional[str] = None
    points: Optional[int] = None
    explanation: Optional[str] = None
    media_url: Optional[str] = None
    media_provider: Optional[str] = None
    options: Optional[List[AuthoredQuestionOption]] = None
    correct_option: Optional[str] = None
    primary_answer: Optional[str] = None
    accepted_answers: Optional[List[str]] = None
    correct_boolean: Optional[bool] = None
    config_override: Optional[Dict[str, Any]] = None


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def get_draft_version_or_404(draft_id: int, db: Session) -> QuizVersion:
    """Finds a QuizVersion by ID ensuring it has status='draft'."""
    version = db.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Qoralama (ID: {draft_id}) topilmadi",
        )
    if version.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bu versiya qoralama holatida emas (status: {version.status})",
        )
    return version


def serialize_draft_structure(version: QuizVersion, db: Session) -> dict:
    """Serializes a draft version with its ordered rounds and questions."""
    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )
    rounds_data = []
    total_questions = 0

    for r in rounds:
        rqs = (
            db.query(RoundQuestion)
            .filter(RoundQuestion.round_id == r.id)
            .order_by(RoundQuestion.sequence.asc())
            .all()
        )
        q_list = []
        for rq in rqs:
            q = rq.question
            pts = rq.points_override if rq.points_override is not None else (q.default_points or q.points or 1)
            accepted = [
                {"id": a.id, "answer_text": a.answer_text, "is_primary": a.is_primary}
                for a in q.accepted_answers
            ]
            primary_ans = next((a.answer_text for a in q.accepted_answers if a.is_primary), None)
            alt_ans = [a.answer_text for a in q.accepted_answers if not a.is_primary]

            q_list.append({
                "round_question_id": rq.id,
                "question_id": q.id,
                "sequence": rq.sequence,
                "text": q.text,
                "points": pts,
                "explanation": q.explanation,
                "media_url": q.media_url,
                "media_provider": q.media_provider,
                "question_type": q.question_type or "text",
                "options": q.options,
                "primary_answer": primary_ans,
                "accepted_answers": alt_ans,
                "all_accepted_answers": accepted,
                "config_override": rq.config_override or {},
            })

        total_questions += len(q_list)
        meta = CANONICAL_ROUND_TYPES.get(r.round_type, {})
        rounds_data.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "name": meta.get("name", r.round_type),
            "description": meta.get("description", ""),
            "config": r.config or {},
            "questions_count": len(q_list),
            "questions": q_list,
        })

    return {
        "quiz_id": version.quiz.id,
        "version_id": version.id,
        "version_number": version.version_number,
        "title": version.quiz.title,
        "description": version.quiz.description,
        "game_mode": version.game_mode,
        "status": version.status,
        "created_at": version.quiz.created_at.isoformat() if version.quiz.created_at else None,
        "total_questions": total_questions,
        "rounds_count": len(rounds_data),
        "rounds": rounds_data,
    }


def validate_and_normalize_authored_question(
    round_type: str,
    payload: AuthoredQuestionCreateRequest,
) -> tuple[str, Optional[list], str, list[str], int, Optional[str], Optional[str], Optional[str]]:
    """
    Validates question payload according to canonical round rules.
    Returns: (question_type, options, primary_answer, alt_answers, points, explanation, media_url, media_provider)
    """
    if not payload.text or not payload.text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Savol matni bo'sh bo'lishi mumkin emas",
        )

    text = payload.text.strip()
    points = payload.points if (payload.points is not None and payload.points > 0) else 1
    explanation = payload.explanation.strip() if payload.explanation and payload.explanation.strip() else None
    media_url = payload.media_url.strip() if payload.media_url and payload.media_url.strip() else None
    media_provider = payload.media_provider or ("image" if media_url else None)

    # 1. Gulmisiz, rayhonmisiz? (MCQ, exactly 4 options, 1 correct option key)
    if round_type == "gulmisiz_rayhonmisiz":
        if not payload.options or len(payload.options) != 4:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Gulmisiz, rayhonmisiz? turi uchun aynan 4 ta variant (A, B, C, D) kiritilishi shart",
            )
        expected_keys = ["A", "B", "C", "D"]
        normalized_options = []
        option_text_map = {}
        for idx, opt in enumerate(payload.options):
            key = expected_keys[idx]
            opt_text = opt.text.strip() if opt.text else ""
            if not opt_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Variant {key} matni bo'sh bo'lishi mumkin emas",
                )
            normalized_options.append({"key": key, "text": opt_text})
            option_text_map[key] = opt_text

        correct_key = (payload.correct_option or "").strip().upper()
        if correct_key not in expected_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="To'g'ri variant A, B, C yoki D bo'lishi shart",
            )

        primary_ans = correct_key
        alt_answers = [option_text_map[correct_key]]
        return ("mcq", normalized_options, primary_ans, alt_answers, points, explanation, media_url, media_provider)

    # 2. Aldama meni (Yes/No, True/False)
    elif round_type == "aldama_meni":
        is_true = None
        if payload.correct_boolean is not None:
            is_true = payload.correct_boolean
        elif payload.primary_answer:
            norm = payload.primary_answer.strip().lower()
            if norm in ["ha", "rost", "to'g'ri", "true"]:
                is_true = True
            elif norm in ["yo'q", "yolg'on", "noto'g'ri", "false"]:
                is_true = False

        if is_true is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Aldama meni turi uchun to'g'ri javob 'ha' (rost) yoki 'yo'q' (yolg'on) sifatida belgilanishi shart",
            )

        if is_true:
            primary_ans = "ha"
            alt_answers = ["rost", "to'g'ri", "true"]
        else:
            primary_ans = "yo'q"
            alt_answers = ["yolg'on", "noto'g'ri", "false"]

        return ("boolean", None, primary_ans, alt_answers, points, explanation, media_url, media_provider)

    # 3. Rasmiyatchilik (Media/Image question)
    elif round_type == "rasmiyatchilik":
        primary_ans = (payload.primary_answer or "").strip()
        if not primary_ans:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Rasmiyatchilik turi uchun to'g'ri javob kiritilishi shart",
            )
        alt_answers = [a.strip() for a in (payload.accepted_answers or []) if a and a.strip()]
        return ("media", None, primary_ans, alt_answers, points, explanation, media_url, media_provider)

    # 4. Zanjir, Mantiqqasqon, Vabank, Svoyak, Zakovat Classic
    else:
        primary_ans = (payload.primary_answer or "").strip()
        if not primary_ans:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Asosiy to'g'ri javob kiritilishi shart",
            )
        alt_answers = [a.strip() for a in (payload.accepted_answers or []) if a and a.strip()]
        return ("text", None, primary_ans, alt_answers, points, explanation, media_url, media_provider)


# =========================================================
# DRAFT LIFECYCLE ENDPOINTS
# =========================================================

@router.post("", status_code=status.HTTP_201_CREATED)
def create_draft(payload: CreateDraftQuizRequest, db: Session = Depends(get_db)):
    """Creates a new empty Quiz container and draft QuizVersion."""
    title = payload.title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Quiz nomi bo'sh bo'lishi mumkin emas",
        )

    quiz = Quiz(
        title=title,
        description=payload.description.strip() if payload.description else None,
    )
    db.add(quiz)
    db.flush()

    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        game_mode=payload.game_mode or "modern_multiround",
        status="draft",
        published_manifest=None,
    )
    db.add(version)
    db.commit()
    db.refresh(quiz)
    db.refresh(version)

    return {
        "success": True,
        "quiz_id": quiz.id,
        "version_id": version.id,
        "version_number": version.version_number,
        "title": quiz.title,
        "game_mode": version.game_mode,
        "status": version.status,
    }


@router.get("", status_code=status.HTTP_200_OK)
def list_drafts(db: Session = Depends(get_db)):
    """Lists all draft quiz versions."""
    draft_versions = (
        db.query(QuizVersion)
        .filter(QuizVersion.status == "draft")
        .order_by(QuizVersion.id.desc())
        .all()
    )
    results = []
    for v in draft_versions:
        rounds_list = []
        total_questions = 0
        for r in v.rounds:
            rqs_count = db.query(RoundQuestion).filter(RoundQuestion.round_id == r.id).count()
            total_questions += rqs_count
            meta = CANONICAL_ROUND_TYPES.get(r.round_type, {})
            rounds_list.append({
                "round_id": r.id,
                "sequence": r.sequence,
                "round_type": r.round_type,
                "name": meta.get("name", r.round_type),
                "questions_count": rqs_count,
            })
        results.append({
            "quiz_id": v.quiz.id,
            "version_id": v.id,
            "version_number": v.version_number,
            "title": v.quiz.title,
            "description": v.quiz.description,
            "game_mode": v.game_mode,
            "status": v.status,
            "created_at": v.quiz.created_at.isoformat() if v.quiz.created_at else None,
            "total_questions": total_questions,
            "rounds_count": len(rounds_list),
            "rounds": rounds_list,
        })
    return results


@router.get("/{draft_id}", status_code=status.HTTP_200_OK)
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    """Loads complete draft details with all rounds and authored questions."""
    version = db.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Kviz versiyasi (ID: {draft_id}) topilmadi",
        )
    return serialize_draft_structure(version, db)


@router.put("/{draft_id}", status_code=status.HTTP_200_OK)
def update_draft(draft_id: int, payload: UpdateDraftQuizRequest, db: Session = Depends(get_db)):
    """Updates draft quiz title, description, or game mode."""
    version = get_draft_version_or_404(draft_id, db)
    quiz = version.quiz

    if payload.title is not None:
        t = payload.title.strip()
        if not t:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Quiz nomi bo'sh bo'lishi mumkin emas",
            )
        quiz.title = t

    if payload.description is not None:
        quiz.description = payload.description.strip() if payload.description.strip() else None

    if payload.game_mode is not None:
        version.game_mode = payload.game_mode.strip()

    db.commit()
    return {
        "success": True,
        "quiz_id": quiz.id,
        "version_id": version.id,
        "title": quiz.title,
        "description": quiz.description,
        "game_mode": version.game_mode,
    }


@router.delete("/{draft_id}", status_code=status.HTTP_200_OK)
def delete_draft(draft_id: int, db: Session = Depends(get_db)):
    """
    Deletes a draft quiz version and its quiz container.
    Cleans up any questions authored exclusively for this draft so that
    no orphaned or dangling questions are left behind.
    Question Bank items (imported/curated) are never deleted.
    """
    version = get_draft_version_or_404(draft_id, db)
    quiz = version.quiz

    # Identify authored questions created exclusively inside this draft version
    draft_questions = (
        db.query(Question)
        .filter(Question.status == "draft")
        .all()
    )
    exclusive_qs = [
        q for q in draft_questions
        if (q.source_meta or {}).get("created_in_builder") is True
        and (q.source_meta or {}).get("draft_id") == version.id
    ]

    # Delete quiz container (cascades to QuizVersion, Round, RoundQuestion)
    db.delete(quiz)
    db.flush()

    # Clean up questions authored exclusively for this draft if not used in any other round
    for q in exclusive_qs:
        has_other_rounds = db.query(RoundQuestion).filter(RoundQuestion.question_id == q.id).count()
        if has_other_rounds == 0:
            db.delete(q)

    db.commit()
    return {"success": True, "message": f"Qoralama (ID: {draft_id}) o'chirildi"}


# =========================================================
# ROUND MANAGEMENT ENDPOINTS
# =========================================================

@router.post("/{draft_id}/rounds", status_code=status.HTTP_201_CREATED)
def add_round(draft_id: int, payload: AddRoundRequest, db: Session = Depends(get_db)):
    """Adds a new canonical round to a draft."""
    version = get_draft_version_or_404(draft_id, db)

    if payload.round_type not in CANONICAL_ROUND_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Noma'lum raund turi: {payload.round_type}",
        )

    # Next sequence
    max_seq = db.query(func.max(Round.sequence)).filter(Round.quiz_version_id == version.id).scalar() or 0
    new_seq = max_seq + 1

    round_obj = Round(
        quiz_version_id=version.id,
        sequence=new_seq,
        round_type=payload.round_type,
        config=payload.config or {},
    )
    db.add(round_obj)
    db.commit()
    db.refresh(round_obj)

    meta = CANONICAL_ROUND_TYPES.get(round_obj.round_type, {})
    return {
        "success": True,
        "round_id": round_obj.id,
        "sequence": round_obj.sequence,
        "round_type": round_obj.round_type,
        "name": meta.get("name", round_obj.round_type),
        "config": round_obj.config,
    }


@router.put("/{draft_id}/rounds/reorder", status_code=status.HTTP_200_OK)
def reorder_rounds(draft_id: int, payload: ReorderRoundsRequest, db: Session = Depends(get_db)):
    """Reorders rounds within a draft version."""
    version = get_draft_version_or_404(draft_id, db)
    rounds = db.query(Round).filter(Round.quiz_version_id == version.id).all()
    round_map = {r.id: r for r in rounds}

    if len(payload.round_ids) != len(rounds) or set(payload.round_ids) != set(round_map.keys()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Barcha raund ID lari to'liq va takrorlanmasdan uzatilishi shart",
        )

    # 2-step renumbering to avoid collision
    for idx, rid in enumerate(payload.round_ids, start=1):
        round_map[rid].sequence = -idx
    db.flush()

    for idx, rid in enumerate(payload.round_ids, start=1):
        round_map[rid].sequence = idx
    db.commit()

    return {"success": True, "ordered_round_ids": payload.round_ids}


@router.put("/{draft_id}/rounds/{round_id}", status_code=status.HTTP_200_OK)
def update_round(draft_id: int, round_id: int, payload: UpdateRoundRequest, db: Session = Depends(get_db)):
    """Updates round configuration or round type."""
    version = get_draft_version_or_404(draft_id, db)
    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    if payload.round_type is not None:
        if payload.round_type not in CANONICAL_ROUND_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Noma'lum raund turi: {payload.round_type}",
            )
        round_obj.round_type = payload.round_type

    if payload.config is not None:
        round_obj.config = payload.config

    db.commit()
    db.refresh(round_obj)
    meta = CANONICAL_ROUND_TYPES.get(round_obj.round_type, {})
    return {
        "success": True,
        "round_id": round_obj.id,
        "sequence": round_obj.sequence,
        "round_type": round_obj.round_type,
        "name": meta.get("name", round_obj.round_type),
        "config": round_obj.config,
    }


@router.delete("/{draft_id}/rounds/{round_id}", status_code=status.HTTP_200_OK)
def delete_round(draft_id: int, round_id: int, db: Session = Depends(get_db)):
    """Deletes a round from draft and re-sequences remaining rounds."""
    version = get_draft_version_or_404(draft_id, db)
    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    # Delete round (cascades RoundQuestion links; does NOT destroy global Question rows)
    db.delete(round_obj)
    db.flush()

    # Re-sequence remaining rounds
    remaining = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )
    for idx, r in enumerate(remaining, start=1):
        r.sequence = idx
    db.commit()

    return {"success": True, "message": f"Raund (ID: {round_id}) o'chirildi"}


# =========================================================
# QUESTION MANAGEMENT ENDPOINTS
# =========================================================

@router.post("/{draft_id}/rounds/{round_id}/questions", status_code=status.HTTP_201_CREATED)
def add_authored_question(
    draft_id: int,
    round_id: int,
    payload: AuthoredQuestionCreateRequest,
    db: Session = Depends(get_db),
):
    """
    Authors a new question from scratch inside a specific round.
    Validates fields against the round's canonical type.
    CRITICAL: Question.round_id remains NULL (attached exclusively through RoundQuestion).
    """
    version = get_draft_version_or_404(draft_id, db)
    round_obj = (
        db.query(Round)
        .filter(Round.id == round_id, Round.quiz_version_id == version.id)
        .first()
    )
    if not round_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    qtype, opts, primary_ans, alts, points, exp, m_url, m_prov = validate_and_normalize_authored_question(
        round_obj.round_type, payload
    )

    # 1. Create standard Question entity (round_id strictly NULL)
    question = Question(
        text=payload.text.strip(),
        points=points,
        default_points=points,
        explanation=exp,
        media_url=m_url,
        media_provider=m_prov,
        question_type=qtype,
        options=opts,
        status="draft",  # Strictly draft status, completely isolated from Question Bank
        round_id=None,  # MUST remain NULL
        sequence=None,  # MUST remain NULL
        source_meta={
            "created_in_builder": True,
            "draft_id": version.id,
            "round_type": round_obj.round_type,
        },
    )
    db.add(question)
    db.flush()

    # 2. Add AcceptedAnswer(s)
    db.add(AcceptedAnswer(question_id=question.id, answer_text=primary_ans, is_primary=True))
    for alt in alts:
        if alt.strip().lower() != primary_ans.strip().lower():
            db.add(AcceptedAnswer(question_id=question.id, answer_text=alt.strip(), is_primary=False))

    # 3. Attach through RoundQuestion
    max_seq = (
        db.query(func.max(RoundQuestion.sequence))
        .filter(RoundQuestion.round_id == round_obj.id)
        .scalar()
        or 0
    )
    rq = RoundQuestion(
        round_id=round_obj.id,
        question_id=question.id,
        sequence=max_seq + 1,
        points_override=None,
        config_override=payload.config_override,
    )
    db.add(rq)
    db.commit()
    db.refresh(rq)
    db.refresh(question)

    return {
        "success": True,
        "round_question_id": rq.id,
        "question_id": question.id,
        "round_id": round_obj.id,
        "sequence": rq.sequence,
        "text": question.text,
        "points": points,
        "question_type": question.question_type,
        "primary_answer": primary_ans,
        "accepted_answers": alts,
    }


@router.put("/{draft_id}/rounds/{round_id}/questions/reorder", status_code=status.HTTP_200_OK)
def reorder_questions(
    draft_id: int,
    round_id: int,
    payload: ReorderQuestionsRequest,
    db: Session = Depends(get_db),
):
    """Reorders questions within a round."""
    version = get_draft_version_or_404(draft_id, db)
    rqs = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.round_id == round_id)
        .all()
    )
    if any(rq.round.quiz_version_id != version.id for rq in rqs):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raund topilmadi")

    rq_map = {rq.id: rq for rq in rqs}
    if len(payload.round_question_ids) != len(rqs) or set(payload.round_question_ids) != set(rq_map.keys()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Barcha savollar to'liq va takrorlanmasdan uzatilishi shart",
        )

    # 2-step renumbering to avoid UniqueConstraint("round_id", "sequence")
    for idx, rq_id in enumerate(payload.round_question_ids, start=1):
        rq_map[rq_id].sequence = -idx
    db.flush()

    for idx, rq_id in enumerate(payload.round_question_ids, start=1):
        rq_map[rq_id].sequence = idx
    db.commit()

    return {"success": True, "ordered_round_question_ids": payload.round_question_ids}


@router.put("/{draft_id}/rounds/{round_id}/questions/{rq_id}", status_code=status.HTTP_200_OK)
def update_authored_question(
    draft_id: int,
    round_id: int,
    rq_id: int,
    payload: AuthoredQuestionUpdateRequest,
    db: Session = Depends(get_db),
):
    """Updates an existing authored question inside a draft round."""
    version = get_draft_version_or_404(draft_id, db)
    rq = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.id == rq_id, RoundQuestion.round_id == round_id)
        .first()
    )
    if not rq or rq.round.quiz_version_id != version.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol raundda topilmadi")

    q = rq.question

    if payload.text is not None:
        t = payload.text.strip()
        if not t:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Savol matni bo'sh bo'lishi mumkin emas",
            )
        q.text = t

    if payload.points is not None and payload.points > 0:
        q.points = payload.points
        q.default_points = payload.points

    if payload.explanation is not None:
        q.explanation = payload.explanation.strip() if payload.explanation.strip() else None

    if payload.media_url is not None:
        q.media_url = payload.media_url.strip() if payload.media_url.strip() else None
        if q.media_url:
            q.media_provider = payload.media_provider or "image"
        else:
            q.media_provider = None

    if payload.config_override is not None:
        rq.config_override = payload.config_override

    # Options update (for MCQ)
    if payload.options is not None:
        if len(payload.options) != 4:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Variantlar soni aynan 4 ta bo'lishi shart",
            )
        expected_keys = ["A", "B", "C", "D"]
        normalized_options = []
        option_text_map = {}
        for idx, opt in enumerate(payload.options):
            key = expected_keys[idx]
            opt_text = opt.text.strip() if opt.text else ""
            if not opt_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Variant {key} matni bo'sh bo'lishi mumkin emas",
                )
            normalized_options.append({"key": key, "text": opt_text})
            option_text_map[key] = opt_text
        q.options = normalized_options

        # If correct option specified, update accepted answers
        if payload.correct_option:
            correct_key = payload.correct_option.strip().upper()
            if correct_key in expected_keys:
                db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q.id).delete()
                db.add(AcceptedAnswer(question_id=q.id, answer_text=correct_key, is_primary=True))
                db.add(AcceptedAnswer(question_id=q.id, answer_text=option_text_map[correct_key], is_primary=False))

    # Answers update (for text/boolean)
    elif payload.primary_answer is not None or payload.correct_boolean is not None:
        prim_ans = None
        alt_answers = []

        if payload.correct_boolean is not None:
            if payload.correct_boolean:
                prim_ans = "ha"
                alt_answers = ["rost", "to'g'ri", "true"]
            else:
                prim_ans = "yo'q"
                alt_answers = ["yolg'on", "noto'g'ri", "false"]
        elif payload.primary_answer:
            prim_ans = payload.primary_answer.strip()
            if payload.accepted_answers:
                alt_answers = [a.strip() for a in payload.accepted_answers if a and a.strip()]

        if prim_ans:
            db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q.id).delete()
            db.add(AcceptedAnswer(question_id=q.id, answer_text=prim_ans, is_primary=True))
            for alt in alt_answers:
                if alt.lower() != prim_ans.lower():
                    db.add(AcceptedAnswer(question_id=q.id, answer_text=alt, is_primary=False))

    db.commit()
    db.refresh(q)
    db.refresh(rq)

    return {
        "success": True,
        "round_question_id": rq.id,
        "question_id": q.id,
        "text": q.text,
        "points": q.points,
        "explanation": q.explanation,
        "options": q.options,
    }


@router.delete("/{draft_id}/rounds/{round_id}/questions/{rq_id}", status_code=status.HTTP_200_OK)
def delete_question_from_round(
    draft_id: int,
    round_id: int,
    rq_id: int,
    db: Session = Depends(get_db),
):
    """
    Removes a question from a draft round by deleting the RoundQuestion association.
    CRITICAL: Does NOT hard-delete the Question entity (Question is global and reusable).
    """
    version = get_draft_version_or_404(draft_id, db)
    rq = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.id == rq_id, RoundQuestion.round_id == round_id)
        .first()
    )
    if not rq or rq.round.quiz_version_id != version.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol raundda topilmadi")

    # Delete only the RoundQuestion link
    db.delete(rq)
    db.flush()

    # Re-sequence remaining questions in the round
    remaining = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.round_id == round_id)
        .order_by(RoundQuestion.sequence.asc())
        .all()
    )
    for idx, item in enumerate(remaining, start=1):
        item.sequence = idx
    db.commit()

    return {"success": True, "message": f"Savol raunddan o'chirildi (ID: {rq_id})"}


# =========================================================
# VALIDATION CORE LOGIC
# =========================================================

def run_publish_validation(version: QuizVersion, db: Session) -> dict:
    """
    Comprehensive publish-readiness validation engine.
    Returns structured field/round/question-level errors and warnings.
    Enforces mode-specific rules:
    - classic_zakovat: exactly 2 rounds x 12 questions (sequences 1..12) as blocking errors.
    - mantiqqasqon: hidden rule/logic required in round config as blocking error.
    - svoyak: point ladder and strictly positive points.
    - modern_multiround: canonical round type requirements.
    """
    structured_errors = []
    structured_warnings = []

    quiz = version.quiz
    if not quiz or not quiz.title or not quiz.title.strip():
        structured_errors.append({
            "scope": "quiz",
            "field": "title",
            "round_id": None,
            "round_sequence": None,
            "round_type": None,
            "question_id": None,
            "question_sequence": None,
            "code": "REQUIRED_TITLE",
            "message": "Kviz nomi kiritilmagan."
        })

    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )

    if not rounds:
        structured_errors.append({
            "scope": "quiz",
            "field": "rounds",
            "round_id": None,
            "round_sequence": None,
            "round_type": None,
            "question_id": None,
            "question_sequence": None,
            "code": "MIN_ROUNDS",
            "message": "Kvizda kamida bitta raund bo'lishi shart."
        })

    total_q = 0

    for r in rounds:
        rqs = (
            db.query(RoundQuestion)
            .filter(RoundQuestion.round_id == r.id)
            .order_by(RoundQuestion.sequence.asc())
            .all()
        )
        total_q += len(rqs)
        meta = CANONICAL_ROUND_TYPES.get(r.round_type, {})
        rname = meta.get("name", r.round_type)

        if not rqs:
            structured_errors.append({
                "scope": "round",
                "field": "questions",
                "round_id": r.id,
                "round_sequence": r.sequence,
                "round_type": r.round_type,
                "question_id": None,
                "question_sequence": None,
                "code": "EMPTY_ROUND",
                "message": f"'{rname}' raundida hech qanday savol yo'q."
            })
            continue

        # Check questions in this round
        for rq in rqs:
            q = rq.question
            if not q or not q.text or not q.text.strip():
                structured_errors.append({
                    "scope": "question",
                    "field": "text",
                    "round_id": r.id,
                    "round_sequence": r.sequence,
                    "round_type": r.round_type,
                    "question_id": q.id if q else None,
                    "question_sequence": rq.sequence,
                    "code": "REQUIRED_TEXT",
                    "message": f"'{rname}' #{rq.sequence}-savol matni bo'sh bo'lishi mumkin emas."
                })

            # Check accepted answers
            primary_ans = next((a for a in q.accepted_answers if a.is_primary), None) if q else None
            if not primary_ans or not primary_ans.answer_text or not primary_ans.answer_text.strip():
                structured_errors.append({
                    "scope": "question",
                    "field": "primary_answer",
                    "round_id": r.id,
                    "round_sequence": r.sequence,
                    "round_type": r.round_type,
                    "question_id": q.id if q else None,
                    "question_sequence": rq.sequence,
                    "code": "MISSING_PRIMARY_ANSWER",
                    "message": f"'{rname}' #{rq.sequence}-savol uchun to'g'ri javob belgilanmagan."
                })

            # Check points
            effective_points = rq.points_override if rq.points_override is not None else (q.default_points if q and q.default_points is not None else (q.points if q else 1))
            if effective_points is None or effective_points <= 0:
                structured_errors.append({
                    "scope": "question",
                    "field": "points",
                    "round_id": r.id,
                    "round_sequence": r.sequence,
                    "round_type": r.round_type,
                    "question_id": q.id if q else None,
                    "question_sequence": rq.sequence,
                    "code": "INVALID_POINTS",
                    "message": f"'{rname}' #{rq.sequence}-savol bali 0 dan katta bo'lishi shart."
                })

            # Round type specific question checks
            if r.round_type == "gulmisiz_rayhonmisiz":
                if not q.options or len(q.options) != 4:
                    structured_errors.append({
                        "scope": "question",
                        "field": "options",
                        "round_id": r.id,
                        "round_sequence": r.sequence,
                        "round_type": r.round_type,
                        "question_id": q.id,
                        "question_sequence": rq.sequence,
                        "code": "MCQ_INVALID_OPTIONS",
                        "message": f"'{rname}' raundidagi #{rq.sequence}-savolda 4 ta variant yo'q."
                    })
                else:
                    opt_keys = [opt.get("key") for opt in q.options if isinstance(opt, dict)]
                    if opt_keys != ["A", "B", "C", "D"]:
                        structured_errors.append({
                            "scope": "question",
                            "field": "options",
                            "round_id": r.id,
                            "round_sequence": r.sequence,
                            "round_type": r.round_type,
                            "question_id": q.id,
                            "question_sequence": rq.sequence,
                            "code": "MCQ_INVALID_KEYS",
                            "message": f"'{rname}' #{rq.sequence}-savol variantlari kalitlari A, B, C, D bo'lishi shart."
                        })
                    if primary_ans and primary_ans.answer_text.upper() not in ["A", "B", "C", "D"]:
                        structured_errors.append({
                            "scope": "question",
                            "field": "options",
                            "round_id": r.id,
                            "round_sequence": r.sequence,
                            "round_type": r.round_type,
                            "question_id": q.id,
                            "question_sequence": rq.sequence,
                            "code": "MCQ_MISSING_CORRECT",
                            "message": f"'{rname}' #{rq.sequence}-savolda to'g'ri variant A, B, C yoki D bo'lishi shart."
                        })

            elif r.round_type == "aldama_meni":
                if primary_ans:
                    norm = primary_ans.answer_text.strip().lower()
                    if norm not in ["ha", "rost", "yo'q", "yolg'on", "true", "false"]:
                        structured_errors.append({
                            "scope": "question",
                            "field": "primary_answer",
                            "round_id": r.id,
                            "round_sequence": r.sequence,
                            "round_type": r.round_type,
                            "question_id": q.id,
                            "question_sequence": rq.sequence,
                            "code": "BOOLEAN_INVALID_ANSWER",
                            "message": f"'{rname}' #{rq.sequence}-savol javobi 'ha' yoki 'yo'q' bo'lishi shart."
                        })

        # Round level checks
        if r.round_type == "mantiqqasqon":
            hidden_rule = (r.config or {}).get("hidden_rule") or (r.config or {}).get("logic")
            if not hidden_rule or not str(hidden_rule).strip():
                structured_errors.append({
                    "scope": "round",
                    "field": "hidden_rule",
                    "round_id": r.id,
                    "round_sequence": r.sequence,
                    "round_type": r.round_type,
                    "question_id": None,
                    "question_sequence": None,
                    "code": "MISSING_HIDDEN_RULE",
                    "message": f"'{rname}' raundi uchun yashirin qoida (hidden_rule) kiritilishi shart."
                })

        elif r.round_type == "zanjir" and len(rqs) > 1:
            for i in range(len(rqs) - 1):
                ans_curr = next((a.answer_text for a in rqs[i].question.accepted_answers if a.is_primary), "")
                ans_next = next((a.answer_text for a in rqs[i+1].question.accepted_answers if a.is_primary), "")
                if ans_curr and ans_next:
                    c_clean = ans_curr.strip().lower()
                    n_clean = ans_next.strip().lower()
                    if c_clean and n_clean and c_clean[-1] != n_clean[0]:
                        structured_warnings.append({
                            "scope": "round",
                            "field": "zanjir_chain",
                            "round_id": r.id,
                            "round_sequence": r.sequence,
                            "round_type": r.round_type,
                            "question_id": rqs[i+1].question_id,
                            "question_sequence": rqs[i+1].sequence,
                            "code": "ZANJIR_DISCONTINUITY",
                            "message": f"Zanjir ogohlantirish: #{rqs[i].sequence} ('...{c_clean[-1]}') va #{rqs[i+1].sequence} ('{n_clean[0]}...') harflari mos tushmayapti."
                        })

        elif r.round_type == "svoyak_theme":
            if len(rqs) != 5:
                structured_warnings.append({
                    "scope": "round",
                    "field": "questions_count",
                    "round_id": r.id,
                    "round_sequence": r.sequence,
                    "round_type": r.round_type,
                    "question_id": None,
                    "question_sequence": None,
                    "code": "SVOYAK_QUESTION_COUNT",
                    "message": f"'{rname}' Svoяk mavzusi uchun 5 ta savol (10, 20, 30, 40, 50 ball) tavsiya etiladi (hozirda {len(rqs)} ta)."
                })

    # Game mode checks
    if version.game_mode == "classic_zakovat":
        # Correction #1: EXACTLY 2 rounds and 12 questions each with sequences 1..12 are BLOCKING ERRORS
        if len(rounds) != 2:
            structured_errors.append({
                "scope": "quiz",
                "field": "rounds",
                "round_id": None,
                "round_sequence": None,
                "round_type": None,
                "question_id": None,
                "question_sequence": None,
                "code": "INVALID_ROUND_COUNT",
                "message": f"Klassik Zakovat formati uchun aynan 2 ta tur bo'lishi shart (hozirda {len(rounds)} ta)."
            })
        for r_idx, r in enumerate(rounds, start=1):
            r_rqs = (
                db.query(RoundQuestion)
                .filter(RoundQuestion.round_id == r.id)
                .order_by(RoundQuestion.sequence.asc())
                .all()
            )
            if len(r_rqs) != 12:
                structured_errors.append({
                    "scope": "round",
                    "field": "questions",
                    "round_id": r.id,
                    "round_sequence": r.sequence,
                    "round_type": r.round_type,
                    "question_id": None,
                    "question_sequence": None,
                    "code": "INVALID_QUESTION_COUNT",
                    "message": f"{r_idx}-turda aynan 12 ta savol bo'lishi shart (hozirda {len(r_rqs)} ta)."
                })
            else:
                seqs = [rq.sequence for rq in r_rqs]
                if seqs != list(range(1, 13)):
                    structured_errors.append({
                        "scope": "round",
                        "field": "sequence",
                        "round_id": r.id,
                        "round_sequence": r.sequence,
                        "round_type": r.round_type,
                        "question_id": None,
                        "question_sequence": None,
                        "code": "SEQUENCE_GAP",
                        "message": f"{r_idx}-turda savollar ketma-ketligi 1 dan 12 gacha to'liq bo'lishi shart."
                    })

    return {
        "valid": len(structured_errors) == 0,
        "errors": [e["message"] for e in structured_errors],
        "structured_errors": structured_errors,
        "warnings": [w["message"] for w in structured_warnings],
        "structured_warnings": structured_warnings,
        "total_rounds": len(rounds),
        "total_questions": total_q,
        "game_mode": version.game_mode,
    }


# =========================================================
# DRAFT VALIDATION ENDPOINT
# =========================================================

@router.post("/{draft_id}/validate", status_code=status.HTTP_200_OK)
def validate_draft(draft_id: int, db: Session = Depends(get_db)):
    """Validates draft completeness and consistency."""
    version = get_draft_version_or_404(draft_id, db)
    return run_publish_validation(version, db)


# =========================================================
# CREATOR PREVIEW ENDPOINTS
# =========================================================

@router.get("/{draft_id}/preview", status_code=status.HTTP_200_OK)
def preview_draft(draft_id: int, db: Session = Depends(get_db)):
    """
    Creator-only player-facing preview of a draft quiz.
    Renders the exact player-facing structure for every supported game mode.
    Strictly strips accepted_answers and explanations.
    Strictly strips hidden_rule from round config.
    """
    version = db.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Qoralama topilmadi")

    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )

    rounds_data = []
    total_q = 0

    for r in rounds:
        rqs = (
            db.query(RoundQuestion)
            .filter(RoundQuestion.round_id == r.id)
            .order_by(RoundQuestion.sequence.asc())
            .all()
        )
        total_q += len(rqs)
        meta = CANONICAL_ROUND_TYPES.get(r.round_type, {})
        questions_data = []

        for rq in rqs:
            q = rq.question
            pts = rq.points_override if rq.points_override is not None else (q.default_points if q.default_points is not None else (q.points or 1))

            # Safe options without correct answer indicators
            safe_options = None
            if q.options and isinstance(q.options, list):
                safe_options = [{"key": opt.get("key"), "text": opt.get("text")} for opt in q.options if isinstance(opt, dict)]

            questions_data.append({
                "round_question_id": rq.id,
                "sequence": rq.sequence,
                "text": q.text,
                "media_provider": q.media_provider,
                "media_url": q.media_url,
                "question_type": q.question_type or "text",
                "options": safe_options,
                "points": pts,
                # NO accepted_answers
                # NO explanation
            })

        # Sanitize config: hidden_rule strictly stripped
        safe_config = sanitize_round_config(r.config or {})

        rounds_data.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "name": meta.get("name", r.round_type),
            "description": meta.get("description", ""),
            "config": safe_config,
            "questions_count": len(questions_data),
            "questions": questions_data,
        })

    return {
        "quiz_id": version.quiz_id,
        "version_id": version.id,
        "version_number": version.version_number,
        "title": version.quiz.title,
        "description": version.quiz.description,
        "game_mode": version.game_mode,
        "status": version.status,
        "total_rounds": len(rounds_data),
        "total_questions": total_q,
        "rounds": rounds_data,
    }


@router.post("/{draft_id}/preview/start", status_code=status.HTTP_201_CREATED)
def start_preview_simulation(draft_id: int, db: Session = Depends(get_db)):
    """
    Starts an isolated preview simulation session for a creator testing their draft.
    Uses SoloAttempt marked as preview (timer_mode='preview').
    Never exposes the draft to public gameplay endpoints or leaderboards.
    """
    version = db.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Qoralama topilmadi")

    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )
    if not rounds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kvizda hech qanday raund yo'q")

    rqs = (
        db.query(RoundQuestion)
        .filter(RoundQuestion.round_id == rounds[0].id)
        .order_by(RoundQuestion.sequence.asc())
        .all()
    )
    if not rqs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Birinchi raundda hech qanday savol yo'q")

    # Create isolated preview attempt
    attempt = SoloAttempt(
        quiz_version_id=version.id,
        timer_mode="preview",  # Isolated preview session flag
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        status="in_progress",
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    # First question state (sanitized)
    first_r = rounds[0]
    first_q = rqs[0].question
    first_pts = rqs[0].points_override if rqs[0].points_override is not None else (first_q.default_points or first_q.points or 1)

    safe_options = None
    if first_q.options and isinstance(first_q.options, list):
        safe_options = [{"key": opt.get("key"), "text": opt.get("text")} for opt in first_q.options if isinstance(opt, dict)]

    safe_r_config = sanitize_round_config(first_r.config or {})

    return {
        "session_token": attempt.session_token,
        "is_preview": True,
        "quiz_title": version.quiz.title,
        "game_mode": version.game_mode,
        "current_state": {
            "status": "in_progress",
            "round": {
                "id": first_r.id,
                "sequence": first_r.sequence,
                "round_type": first_r.round_type,
                "config": safe_r_config,
            },
            "question": {
                "id": first_q.id,
                "round_question_id": rqs[0].id,
                "sequence": rqs[0].sequence,
                "text": first_q.text,
                "media_provider": first_q.media_provider,
                "media_url": first_q.media_url,
                "question_type": first_q.question_type or "text",
                "options": safe_options,
                "points": first_pts,
            },
            "round_question_index": 0,
            "round_total_questions": len(rqs),
        }
    }


# =========================================================
# PUBLISHING ENDPOINT
# =========================================================

class PublishDraftRequest(BaseModel):
    visibility: Optional[str] = "public"
    category: Optional[str] = None


@router.post("/{draft_id}/publish", status_code=status.HTTP_200_OK)
def publish_draft(draft_id: int, payload: Optional[PublishDraftRequest] = None, db: Session = Depends(get_db)):
    """
    Explicit atomic publishing endpoint.
    - Idempotency guard: rejects subsequent publish attempts on already published versions.
    - Runs complete server-side publish-readiness validation; invalid drafts are rejected.
    - Atomically compiles self-contained published_manifest and sets status='published'.
    - Supports visibility: 'public' (default, discoverable) or 'unlisted' (playable by direct link only).
    - Supports category: sets manifest category for Arena filtering.
    """
    version = db.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Kviz versiyasi (ID: {draft_id}) topilmadi",
        )

    # Idempotency safety: already published cannot be published again
    if version.status == "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bu kviz versiyasi allaqachon e'lon qilingan va o'zgartirib bo'lmaydi",
        )
    if version.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Faqat qoralama (draft) holatidagi kvizlarni nashr qilish mumkin (joriy holat: {version.status})",
        )

    # Run comprehensive server-side publish-readiness validation
    val_report = run_publish_validation(version, db)
    if not val_report["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Kviz nashr qilish uchun to'liq emas yoki xatolar mavjud",
                "errors": val_report["errors"],
                "structured_errors": val_report["structured_errors"],
            },
        )

    # Atomic publication transaction
    manifest = compile_published_manifest(version, db)
    vis = (payload.visibility if payload and payload.visibility else "public").strip().lower()
    if vis not in ("public", "unlisted"):
        vis = "public"
    manifest["visibility"] = vis

    cat = (payload.category.strip() if payload and payload.category and payload.category.strip() else None)
    if not cat:
        cat = manifest.get("category") or "Umumiy"
    manifest["category"] = cat

    now_utc = datetime.now(timezone.utc)

    version.published_manifest = manifest
    version.status = "published"
    version.published_at = now_utc

    db.commit()
    db.refresh(version)

    return {
        "success": True,
        "quiz_id": version.quiz_id,
        "version_id": version.id,
        "version_number": version.version_number,
        "title": version.quiz.title,
        "game_mode": version.game_mode,
        "status": version.status,
        "visibility": manifest.get("visibility", "public"),
        "category": manifest.get("category", "Umumiy"),
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "total_rounds": len(manifest.get("rounds", [])),
        "total_questions": sum(len(r.get("questions", [])) for r in manifest.get("rounds", [])),
    }
