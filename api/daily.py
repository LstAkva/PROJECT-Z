from datetime import date
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from database import get_db
from models import DailyQuestion, DailySubmission, User
from api.auth import get_current_user_optional, get_current_user_required
from services.daily_question import (
    get_tashkent_today,
    get_or_schedule_daily_question,
    submit_daily_answer,
    is_repeated_daily_answer,
    MAX_DAILY_ATTEMPTS,
)
from services.daily_engagement import compute_daily_streak, build_share_text

router = APIRouter(prefix="/api/daily", tags=["daily_question"])


def get_client_ip(request: Request) -> str:
    """Extracts client IP from proxy headers or socket."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


class DailySubmissionRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=500, description="Foydalanuvchi javobi")


@router.get("/today")
def get_today_daily_question(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Returns today's shared Daily Question based on Asia/Tashkent calendar date.
    Reveals the correct answer only if the current user has already submitted.
    """
    today = get_tashkent_today()
    daily_q = get_or_schedule_daily_question(db, target_date=today)

    if not daily_q:
        return {
            "status": "unavailable",
            "date": today.isoformat(),
            "message": "Bugun uchun savol mavjud emas. Yangi savollar tez orada qo'shiladi.",
            "question": None
        }

    # Check if authenticated user has already submitted
    user_submission = None
    has_submitted = False
    if current_user:
        sub = (
            db.query(DailySubmission)
            .filter(
                DailySubmission.daily_question_id == daily_q.id,
                DailySubmission.user_id == current_user.id
            )
            .first()
        )
        if sub:
            meta = sub.submission_meta or {}
            attempts = meta.get("attempts", 1)
            history = meta.get("history", [sub.submitted_text])
            is_finished = sub.is_correct or meta.get("is_finished", False) or (attempts >= 3)
            has_submitted = is_finished
            user_submission = {
                "id": sub.id,
                "submitted_text": sub.submitted_text,
                "is_correct": sub.is_correct,
                "attempts_used": attempts,
                "attempts_remaining": max(0, 3 - attempts),
                "history": history,
                "is_finished": is_finished,
                "answered_at": sub.answered_at.isoformat()
            }

    # Streak (signed-in players only; derived from existing submissions, server date only)
    streak = compute_daily_streak(db, current_user.id, today) if current_user else None

    response_payload = {
        "status": "available",
        "date": daily_q.schedule_date.isoformat(),
        "question": {
            "id": daily_q.id,
            "question_id": daily_q.question_id,
            "text": daily_q.question_text,
            "created_at": daily_q.created_at.isoformat(),
        },
        "has_submitted": has_submitted,
        "user_submission": user_submission,
        "is_authenticated": current_user is not None,
        "max_attempts": 3,
        "streak": streak,
    }

    # Reveal original answer and explanation ONLY if finished!
    if has_submitted:
        response_payload["question"]["correct_answer"] = daily_q.primary_answer
        response_payload["question"]["explanation"] = daily_q.explanation
        # Text for "Natijani ulashish": date, outcome and streak only (never the question or answer)
        response_payload["share_text"] = build_share_text(
            schedule_date=daily_q.schedule_date,
            is_correct=bool(user_submission["is_correct"]),
            attempts_used=user_submission.get("attempts_used"),
            max_attempts=MAX_DAILY_ATTEMPTS,
            current_streak=(streak or {}).get("current", 0),
        )

    return response_payload


@router.get("/stats")
def get_daily_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    """The signed-in player's Daily streak: current, best, participation days, last date."""
    return compute_daily_streak(db, current_user.id, get_tashkent_today())


@router.post("/submit")
def submit_today_answer(
    payload: DailySubmissionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    """
    Submits an answer for today's Daily Question.
    Allows up to 3 tries per user per day.
    Reveals the correct answer only upon correct submission or after 3 failed tries.
    """
    today = get_tashkent_today()
    daily_q = get_or_schedule_daily_question(db, target_date=today)

    if not daily_q:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bugun uchun faol Kun Savoli mavjud emas."
        )

    # Check if user already finished all attempts
    existing = (
        db.query(DailySubmission)
        .filter(
            DailySubmission.daily_question_id == daily_q.id,
            DailySubmission.user_id == current_user.id
        )
        .first()
    )
    if existing:
        meta = existing.submission_meta or {}
        attempts = meta.get("attempts", 1)
        history = meta.get("history", [existing.submitted_text])
        is_finished = existing.is_correct or meta.get("is_finished", False) or (attempts >= 3)
        if is_finished:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Siz bugungi barcha urinishlaringizdan foydalandingiz.",
                    "already_submitted": True,
                    "is_correct": existing.is_correct,
                    "submitted_text": existing.submitted_text,
                    "correct_answer": daily_q.primary_answer,
                    "explanation": daily_q.explanation,
                    "answered_at": existing.answered_at.isoformat(),
                    "attempts_used": attempts,
                    "history": history
                }
            )

        # Same answer as an earlier try: tell the player, do not spend an attempt.
        if is_repeated_daily_answer(payload.answer, history):
            return {
                "status": "duplicate",
                "is_correct": False,
                "is_finished": False,
                "attempts_used": attempts,
                "attempts_remaining": max(0, 3 - attempts),
                "history": history,
                "submitted_text": existing.submitted_text,
                "answered_at": existing.answered_at.isoformat(),
                "message": "Bu javobni allaqachon sinab ko‘rgansiz. Boshqa variant kiriting — urinish sarflanmadi.",
            }

    client_ip = get_client_ip(request)
    submission, already_finished = submit_daily_answer(
        db=db,
        daily_question=daily_q,
        user=current_user,
        submitted_text=payload.answer,
        client_ip=client_ip
    )

    if already_finished:
        meta = submission.submission_meta or {}
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Siz bugungi barcha urinishlaringizdan foydalandingiz.",
                "already_submitted": True,
                "is_correct": submission.is_correct,
                "submitted_text": submission.submitted_text,
                "correct_answer": daily_q.primary_answer,
                "explanation": daily_q.explanation,
                "answered_at": submission.answered_at.isoformat(),
                "attempts_used": meta.get("attempts", 3),
                "history": meta.get("history", [submission.submitted_text])
            }
        )

    meta = submission.submission_meta or {}
    attempts_used = meta.get("attempts", 1)
    attempts_remaining = max(0, 3 - attempts_used)
    is_finished = meta.get("is_finished", False)
    history = meta.get("history", [submission.submitted_text])

    res = {
        "status": "success" if submission.is_correct else ("finished" if is_finished else "incorrect"),
        "is_correct": submission.is_correct,
        "is_finished": is_finished,
        "attempts_used": attempts_used,
        "attempts_remaining": attempts_remaining,
        "history": history,
        "submitted_text": submission.submitted_text,
        "answered_at": submission.answered_at.isoformat()
    }

    if is_finished:
        res["correct_answer"] = daily_q.primary_answer
        res["explanation"] = daily_q.explanation
        if submission.is_correct:
            res["message"] = f"To'g'ri javob! {attempts_used}-urinishda topdingiz!"
        else:
            res["message"] = "Afsuski, 3 ta urinishingiz ham tugadi."
    else:
        res["message"] = f"Noto'g'ri javob. Yana {attempts_remaining} ta urinishingiz bor!"

    return res


@router.get("/history")
def get_user_daily_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    """Returns the authenticated user's daily question submission history."""
    submissions = (
        db.query(DailySubmission)
        .join(DailyQuestion, DailySubmission.daily_question_id == DailyQuestion.id)
        .filter(DailySubmission.user_id == current_user.id)
        .order_by(DailyQuestion.schedule_date.desc())
        .all()
    )

    today = get_tashkent_today()
    history = []
    for s in submissions:
        dq = s.daily_question
        meta = s.submission_meta or {}
        attempts = meta.get("attempts", 1)
        finished = s.is_correct or meta.get("is_finished", False) or attempts >= MAX_DAILY_ATTEMPTS
        # Same rule as /today: the answer is revealed only once the player has finished,
        # or once that day is over. (It used to leak today's answer after one wrong try.)
        revealed = finished or dq.schedule_date < today
        history.append({
            "date": dq.schedule_date.isoformat(),
            "question_text": dq.question_text,
            "primary_answer": dq.primary_answer if revealed else None,
            "explanation": dq.explanation if revealed else None,
            "submitted_text": s.submitted_text,
            "is_correct": s.is_correct,
            "is_finished": finished,
            "answered_at": s.answered_at.isoformat()
        })

    return {
        "total_attempted": len(history),
        "total_correct": sum(1 for h in history if h["is_correct"]),
        "history": history
    }
