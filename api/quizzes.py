from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion

router = APIRouter(prefix="/api/quizzes", tags=["quizzes"])


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