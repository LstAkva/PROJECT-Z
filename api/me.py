"""Signed-in player's own progress (read-only): pack progress for the Arena and the game to resume."""
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from api.auth import enforce_rate_limit, get_current_user_optional, get_current_user_required
from database import get_db
from models import AnswerRecord, DailyQuestion, DailySubmission, Question, QuizVersion, SoloAttempt, User
from services.player_progress import pack_progress, resumable_game

router = APIRouter(prefix="/api/me", tags=["player_progress"])


@router.get("/pack-progress")
def get_pack_progress(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """{quiz_id: progress} for packs this player has touched. Only ever the caller's own attempts."""
    progress = pack_progress(db, current_user.id)
    return {"packs": {str(quiz_id): data for quiz_id, data in progress.items()}}


@router.get("/active-game")
def get_active_game(
    quiz_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """The caller's most recent resumable game (optionally for one pack). Never creates or changes a game.
    Anonymous visitors simply get no game (the homepage asks on every visit; a 401 would only add noise)."""
    if current_user is None:
        return {"game": None}
    return {"game": resumable_game(db, current_user.id, quiz_id=quiz_id)}


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


@router.get("/export")
def export_my_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Everything the platform stores about the signed-in player, as one JSON file (right to know,
    O‘RQ-547, article 30). Only the caller's own records. Never: the password hash, sign-in tokens,
    other players, or correct answers (so no question can be spoiled)."""
    enforce_rate_limit(request, "data_export", max_requests=5, window_seconds=300)
    u = current_user
    attempts = (db.query(SoloAttempt).filter(SoloAttempt.user_id == u.id)
                .order_by(SoloAttempt.id.asc()).all())
    answers_by_attempt = {}
    if attempts:
        rows = (db.query(AnswerRecord, Question.text)
                .outerjoin(Question, Question.id == AnswerRecord.question_id)
                .filter(AnswerRecord.attempt_id.in_([a.id for a in attempts]))
                .order_by(AnswerRecord.id.asc()).all())
        for rec, q_text in rows:
            answers_by_attempt.setdefault(rec.attempt_id, []).append({
                "question": q_text,
                "your_answer": rec.submitted_text,
                "correct": rec.is_correct,
                "points": rec.points_awarded,
                "answered_at": _iso(rec.answered_at),
            })

    def kind(a: SoloAttempt) -> str:
        meta = a.attempt_metadata or {}
        if meta.get("is_demo"):
            return "owner_demo"
        if str(a.timer_mode or "").startswith("preview") or meta.get("is_staging"):
            return "preview"
        if meta.get("competitive") is False or a.timer_mode == "no_timer":
            return "practice"
        return "ranked"

    games = []
    for a in attempts:
        version = db.query(QuizVersion).filter(QuizVersion.id == a.quiz_version_id).first()
        title = ((version.published_manifest or {}).get("title") if version else None) or (
            version.quiz.title if version and version.quiz else None)
        games.append({
            "pack": title,
            "format": version.game_mode if version else None,
            "type": kind(a),
            "status": a.status,
            "score": a.total_score,
            "correct_answers": a.total_correct,
            "active_time_seconds": a.active_time_seconds,
            "started_at": _iso(a.started_at),
            "completed_at": _iso(a.completed_at),
            "answers": answers_by_attempt.get(a.id, []),
        })

    daily = []
    for sub, day in (db.query(DailySubmission, DailyQuestion.schedule_date)
                     .join(DailyQuestion, DailyQuestion.id == DailySubmission.daily_question_id)
                     .filter(DailySubmission.user_id == u.id)
                     .order_by(DailySubmission.answered_at.asc()).all()):
        daily.append({"date": _iso(day), "your_answer": sub.submitted_text, "correct": sub.is_correct,
                      "answered_at": _iso(sub.answered_at)})

    data = {
        "about": ("ZakoWhat platformasi siz haqingizda saqlayotgan barcha ma’lumotlar. Parol (faqat xeshlangan "
                  "ko‘rinishda saqlanadi), tizimga kirish tokenlari va boshqa o‘yinchilar ma’lumotlari kiritilmagan."),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format_version": 1,
        "account": {
            "email": u.email,
            "display_name": u.display_name,
            "username": u.username,
            "bio": u.bio,
            "avatar_url": u.avatar_url,
            "profile_public": bool(u.is_public if u.is_public is not None else True),
            "show_statistics": bool(u.show_stats if u.show_stats is not None else True),
            "theme": u.theme_preference,
            "cookie_choice": u.cookie_consent,
            "cookie_choice_recorded_at": _iso(u.consent_recorded_at),
            "terms_accepted_at": _iso(u.terms_accepted_at),
            "terms_version": u.terms_version,
            "sign_in_method": u.auth_provider,
            "has_password": bool(u.hashed_password),
            "linked_google_account": bool(u.google_id),
            "created_at": _iso(u.created_at),
        },
        "games": games,
        "daily_question": daily,
    }
    filename = f"zakowhat-ma-lumotlarim-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
