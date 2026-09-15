from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from database import get_db
from models import Quiz, QuizVersion, Round, Question

router = APIRouter(prefix="/api/quizzes", tags=["quizzes"])

def sanitize_round_config(round_obj: Round) -> dict:
    """Strips secret configuration keys (like hidden_rule) from public display."""
    if not round_obj.config:
        return {}
    config = dict(round_obj.config)
    # Never leak hidden rules to the discovery/gameplay endpoint before resolution
    config.pop("hidden_rule", None)
    return config

@router.get("", status_code=status.HTTP_200_OK)
def list_quizzes(db: Session = Depends(get_db)):
    """Returns published quizzes available for discovery."""
    quizzes = db.query(Quiz).all()
    results = []

    for quiz in quizzes:
        # Get the latest published version
        latest_version = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id)
            .order_by(QuizVersion.version_number.desc())
            .first()
        )
        if not latest_version:
            continue

        results.append({
            "quiz_id": quiz.id,
            "title": quiz.title,
            "description": quiz.description,
            "version_number": latest_version.version_number,
            "published_at": latest_version.published_at.isoformat() if latest_version.published_at else None,
        })

    return results

@router.get("/{quiz_id}", status_code=status.HTTP_200_OK)
def get_quiz_detail(quiz_id: int, db: Session = Depends(get_db)):
    """Returns the ordered structure of a quiz for display and play, without accepted answers."""
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

    # Fetch the latest published version
    latest_version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id)
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not latest_version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published version found for this quiz")

    # Load rounds ordered by sequence, eagerly loading questions to prevent N+1 queries
    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == latest_version.id)
        .order_by(Round.sequence.asc())
        .options(joinedload(Round.questions))
        .all()
    )

    rounds_data = []
    for r in rounds:
        # Explicitly order questions by sequence
        sorted_questions = sorted(r.questions, key=lambda q: q.sequence)
        
        rounds_data.append({
            "id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "config": sanitize_round_config(r),
            "questions": [
                {
                    "id": q.id,
                    "sequence": q.sequence,
                    "text": q.text,
                    "media_provider": q.media_provider,
                    "media_url": q.media_url,
                    "points": q.points,
                    # AcceptedAnswer is intentionally omitted
                }
                for q in sorted_questions
            ],
        })

    return {
        "quiz_id": quiz.id,
        "title": quiz.title,
        "description": quiz.description,
        "version_number": latest_version.version_number,
        "published_at": latest_version.published_at.isoformat() if latest_version.published_at else None,
        "rounds": rounds_data,
    }