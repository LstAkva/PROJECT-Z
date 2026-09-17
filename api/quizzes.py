from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion

router = APIRouter(prefix="/api/quizzes", tags=["quizzes"])

# Canonical agreed round types
CANONICAL_ROUND_TYPES = {
    "gulmisiz_rayhonmisiz": {
        "id": "gulmisiz_rayhonmisiz",
        "name": "1. Gulmisiz, rayhonmisiz?",
        "description": "Variantli savollar / To'g'ri-noto'g'ri tanlovi",
    },
    "zanjir": {
        "id": "zanjir",
        "name": "2. Zanjir",
        "description": "Har bir javob oldingi javobning oxirgi harfidan boshlanadi",
    },
    "mantiqqasqon": {
        "id": "mantiqqasqon",
        "name": "3. Mantiqqasqon",
        "description": "Savollar orasidagi yashirin mantiqiy bog'liqlik",
    },
    "aldama_meni": {
        "id": "aldama_meni",
        "name": "4. Aldama meni",
        "description": "Chalg'ituvchi faktlar va savollar",
    },
    "rasmiyatchilik": {
        "id": "rasmiyatchilik",
        "name": "5. Rasmiyatchilik",
        "description": "Aniq faktologiya va rasmiy ma'lumotlar",
    },
    "vabank": {
        "id": "vabank",
        "name": "6. Vabank",
        "description": "Tavakkal turi / Ballni oshirish yoki yo'qotish",
    },
    # Mode-specific canonical types:
    "svoyak_theme": {
        "id": "svoyak_theme",
        "name": "Svoяk Mavzusi",
        "description": "Svoyak mavzuli turlari (10, 20, 30, 40, 50 ball)",
    },
    "zakovat_classic": {
        "id": "zakovat_classic",
        "name": "Klassik Zakovat",
        "description": "Klassik 12+12 formatidagi savollar",
    },
    # Legacy / compatibility:
    "standard": {
        "id": "standard",
        "name": "Standart",
        "description": "Standart savollar turi",
    },
}


class DraftRoundQuestionItem(BaseModel):
    question_id: int
    sequence: Optional[int] = None
    points_override: Optional[int] = None
    config_override: Optional[Dict[str, Any]] = None


class DraftRoundItem(BaseModel):
    sequence: int
    round_type: str
    config: Optional[Dict[str, Any]] = None
    questions: Optional[List[DraftRoundQuestionItem]] = None
    question_ids: Optional[List[int]] = None


class CreateQuizDraftRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    game_mode: Optional[str] = "modern_multiround"
    round_type: Optional[str] = None
    round_config: Optional[Dict[str, Any]] = None
    question_ids: Optional[List[int]] = None
    rounds: Optional[List[DraftRoundItem]] = None



def sanitize_round_config(config: dict) -> dict:
    """Strips secret configuration keys (like hidden_rule) from public display."""
    if not config:
        return {}
    c = dict(config)
    c.pop("hidden_rule", None)
    return c


def compile_published_manifest(version: QuizVersion, db: Session) -> dict:
    """Compiles the complete immutable playable snapshot for a published quiz version."""
    db.flush()
    rounds_data = []
    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )

    for r in rounds:
        rqs = (
            db.query(RoundQuestion)
            .filter(RoundQuestion.round_id == r.id)
            .order_by(RoundQuestion.sequence.asc())
            .all()
        )
        questions_data = []
        if rqs:
            for rq in rqs:
                q = rq.question
                pts = (
                    rq.points_override
                    if rq.points_override is not None
                    else (q.default_points if q.default_points is not None else q.points)
                )
                accepted = [
                    {"id": a.id, "answer_text": a.answer_text, "is_primary": a.is_primary}
                    for a in q.accepted_answers
                ]
                questions_data.append({
                    "round_question_id": rq.id,
                    "question_id": q.id,
                    "sequence": rq.sequence,
                    "text": q.text,
                    "explanation": q.explanation,
                    "media_provider": q.media_provider,
                    "media_url": q.media_url,
                    "question_type": q.question_type or "text",
                    "options": q.options,
                    "points": pts if pts is not None else 1,
                    "config": rq.config_override or {},
                    "accepted_answers": accepted,
                })
        else:
            # Fallback for legacy rounds without round_questions
            legacy_qs = (
                db.query(Question)
                .filter(Question.round_id == r.id)
                .order_by(Question.sequence.asc())
                .all()
            )
            for q in legacy_qs:
                accepted = [
                    {"id": a.id, "answer_text": a.answer_text, "is_primary": a.is_primary}
                    for a in q.accepted_answers
                ]
                questions_data.append({
                    "round_question_id": None,
                    "question_id": q.id,
                    "sequence": q.sequence or 1,
                    "text": q.text,
                    "explanation": q.explanation,
                    "media_provider": q.media_provider,
                    "media_url": q.media_url,
                    "question_type": q.question_type or "text",
                    "options": q.options,
                    "points": q.points or 1,
                    "config": {},
                    "accepted_answers": accepted,
                })

        rounds_data.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "config": r.config or {},
            "questions": questions_data,
        })

    return {
        "version_id": version.id,
        "quiz_id": version.quiz_id,
        "version_number": version.version_number,
        "game_mode": version.game_mode or "modern_multiround",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "rounds": rounds_data,
    }


@router.get("", status_code=status.HTTP_200_OK)
def list_quizzes(db: Session = Depends(get_db)):
    quizzes = db.query(Quiz).all()
    results = []
    for quiz in quizzes:
        latest_published = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
            .order_by(QuizVersion.version_number.desc())
            .first()
        )
        if latest_published:
            results.append({
                "quiz_id": quiz.id,
                "title": quiz.title,
                "description": quiz.description,
                "game_mode": latest_published.game_mode,
                "version_number": latest_published.version_number,
            })
    return results


@router.get("/canonical-rounds", status_code=status.HTTP_200_OK)
def get_canonical_rounds():
    """Returns the agreed canonical modern multi-round types and supported quiz modes."""
    return list(CANONICAL_ROUND_TYPES.values())


@router.get("/drafts", status_code=status.HTTP_200_OK)
def list_draft_quizzes(db: Session = Depends(get_db)):
    """Returns list of draft quiz versions created by editors."""
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
            rounds_list.append({
                "round_id": r.id,
                "sequence": r.sequence,
                "round_type": r.round_type,
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
            "rounds": rounds_list,
        })
    return results


@router.post("/draft", status_code=status.HTTP_201_CREATED)
def create_quiz_draft(payload: CreateQuizDraftRequest, db: Session = Depends(get_db)):
    """Creates a new Quiz Draft with Quiz and QuizVersion (status='draft', published_manifest=None),
    attaching questions via RoundQuestion links. Questions in the Question Bank remain untouched (round_id=None).
    """
    if not payload.title or not payload.title.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Quiz nomi bo'sh bo'lishi mumkin emas",
        )

    # Normalize rounds structure
    rounds_to_create = []
    if payload.rounds and len(payload.rounds) > 0:
        for r in payload.rounds:
            if r.round_type not in CANONICAL_ROUND_TYPES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Noma'lum raund turi: {r.round_type}",
                )
            q_ids = []
            if r.questions:
                q_ids = [q.question_id for q in r.questions]
            elif r.question_ids:
                q_ids = r.question_ids
            if not q_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Raund {r.sequence} da kamida bitta savol bo'lishi shart",
                )
            rounds_to_create.append({
                "sequence": r.sequence,
                "round_type": r.round_type,
                "config": r.config or {},
                "question_ids": q_ids,
            })
    else:
        # Single round payload
        if not payload.round_type:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Raund turi ko'rsatilishi shart",
            )
        if payload.round_type not in CANONICAL_ROUND_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Noma'lum raund turi: {payload.round_type}",
            )
        if not payload.question_ids or len(payload.question_ids) == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Kamida bitta savol tanlanishi shart",
            )
        rounds_to_create.append({
            "sequence": 1,
            "round_type": payload.round_type,
            "config": payload.round_config or {},
            "question_ids": payload.question_ids,
        })

    # Validate all referenced questions exist
    all_q_ids = []
    for r in rounds_to_create:
        all_q_ids.extend(r["question_ids"])
    unique_q_ids = set(all_q_ids)

    found_qs = db.query(Question).filter(Question.id.in_(unique_q_ids)).all()
    found_map = {q.id: q for q in found_qs}
    missing_ids = [qid for qid in unique_q_ids if qid not in found_map]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Quyidagi savol ID lari topilmadi: {missing_ids}",
        )

    # Create Quiz container
    quiz = Quiz(
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
    )
    db.add(quiz)
    db.flush()

    # Create Draft Version (published_manifest MUST remain None)
    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        game_mode=payload.game_mode or "modern_multiround",
        status="draft",
        published_manifest=None,
    )
    db.add(version)
    db.flush()

    total_q_count = 0
    # Create Rounds and RoundQuestion entries
    for r_data in rounds_to_create:
        round_obj = Round(
            quiz_version_id=version.id,
            sequence=r_data["sequence"],
            round_type=r_data["round_type"],
            config=r_data["config"],
        )
        db.add(round_obj)
        db.flush()

        for idx, q_id in enumerate(r_data["question_ids"], start=1):
            q_obj = found_map[q_id]
            rq = RoundQuestion(
                round_id=round_obj.id,
                question_id=q_obj.id,
                sequence=idx,
                points_override=None,
                config_override=None,
            )
            db.add(rq)
            total_q_count += 1

    db.commit()
    db.refresh(quiz)
    db.refresh(version)

    return {
        "success": True,
        "quiz_id": quiz.id,
        "version_id": version.id,
        "version_number": version.version_number,
        "status": version.status,
        "title": quiz.title,
        "rounds_count": len(rounds_to_create),
        "questions_count": total_q_count,
    }


@router.post("/{quiz_id}/publish/{version_number}", status_code=status.HTTP_200_OK)
def publish_quiz_version(quiz_id: int, version_number: int, db: Session = Depends(get_db)):
    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.version_number == version_number)
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    manifest = compile_published_manifest(version, db)
    version.published_manifest = manifest
    version.status = "published"
    version.published_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": f"Version {version_number} published"}


@router.get("/{quiz_id}", status_code=status.HTTP_200_OK)
def get_quiz_detail(quiz_id: int, db: Session = Depends(get_db)):
    """Returns the ordered structure of a quiz for display and play, without accepted answers."""
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

    latest_version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not latest_version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published version found for this quiz")

    # If published_manifest is present, read directly from immutable snapshot
    if latest_version.published_manifest and "rounds" in latest_version.published_manifest:
        manifest_rounds = latest_version.published_manifest["rounds"]
        rounds_data = []
        for mr in manifest_rounds:
            clean_questions = []
            for mq in mr.get("questions", []):
                clean_questions.append({
                    "id": mq.get("question_id"),
                    "round_question_id": mq.get("round_question_id"),
                    "sequence": mq.get("sequence"),
                    "text": mq.get("text"),
                    "media_provider": mq.get("media_provider"),
                    "media_url": mq.get("media_url"),
                    "question_type": mq.get("question_type", "text"),
                    "options": mq.get("options"),
                    "points": mq.get("points", 1),
                })
            rounds_data.append({
                "id": mr.get("round_id"),
                "sequence": mr.get("sequence"),
                "round_type": mr.get("round_type"),
                "config": sanitize_round_config(mr.get("config", {})),
                "questions": clean_questions,
            })

        return {
            "quiz_id": quiz.id,
            "title": quiz.title,
            "description": quiz.description,
            "game_mode": latest_version.game_mode,
            "version_number": latest_version.version_number,
            "published_at": latest_version.published_at.isoformat() if latest_version.published_at else None,
            "rounds": rounds_data,
        }

    # Live relational fallback (for legacy versions before manifest was introduced)
    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == latest_version.id)
        .order_by(Round.sequence.asc())
        .all()
    )

    rounds_data = []
    for r in rounds:
        rqs = (
            db.query(RoundQuestion)
            .filter(RoundQuestion.round_id == r.id)
            .order_by(RoundQuestion.sequence.asc())
            .all()
        )
        if rqs:
            q_list = []
            for rq in rqs:
                q = rq.question
                pts = (
                    rq.points_override
                    if rq.points_override is not None
                    else (q.default_points if q.default_points is not None else q.points)
                )
                q_list.append({
                    "id": q.id,
                    "round_question_id": rq.id,
                    "sequence": rq.sequence,
                    "text": q.text,
                    "media_provider": q.media_provider,
                    "media_url": q.media_url,
                    "question_type": q.question_type or "text",
                    "options": q.options,
                    "points": pts if pts is not None else 1,
                })
        else:
            sorted_questions = sorted(r.questions, key=lambda q: q.sequence or 0)
            q_list = [
                {
                    "id": q.id,
                    "round_question_id": None,
                    "sequence": q.sequence,
                    "text": q.text,
                    "media_provider": q.media_provider,
                    "media_url": q.media_url,
                    "question_type": q.question_type or "text",
                    "options": q.options,
                    "points": q.points,
                }
                for q in sorted_questions
            ]

        rounds_data.append({
            "id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "config": sanitize_round_config(r.config or {}),
            "questions": q_list,
        })

    return {
        "quiz_id": quiz.id,
        "title": quiz.title,
        "description": quiz.description,
        "game_mode": latest_version.game_mode,
        "version_number": latest_version.version_number,
        "published_at": latest_version.published_at.isoformat() if latest_version.published_at else None,
        "rounds": rounds_data,
    }