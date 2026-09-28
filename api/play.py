import os
import string
import secrets
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, AnswerRecord, User
from services.gameplay import normalize_uzbek_latin, verify_zanjir_chain, is_true_false_match
from api.auth import get_current_user_optional, get_current_user_required, is_secure_cookie, get_authoritative_anon_id

router = APIRouter(prefix="/api/play", tags=["play"])


class AnswerSubmission(BaseModel):
    answer: str
    is_wager: bool = False
    question_id: Optional[int] = None
    round_question_id: Optional[int] = None
    client_submission_id: Optional[str] = None


class StartPlayRequest(BaseModel):
    timer_mode: Optional[str] = "with_timer"


def sanitize_config(config: Optional[dict]) -> dict:
    if not config:
        return {}
    c = dict(config)
    c.pop("hidden_rule", None)
    return c


# =========================================================
# MANIFEST / DB RESOLUTION HELPERS
# =========================================================

def get_manifest_or_db_rounds(version: QuizVersion, db: Session) -> List[Dict[str, Any]]:
    """Returns a unified representation of rounds either from published_manifest or DB."""
    if version.published_manifest and "rounds" in version.published_manifest:
        return version.published_manifest["rounds"]

    # Published quizzes MUST use published_manifest; never fall back to mutable DB
    if version.status == "published":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Nashr qilingan kviz manifesti mavjud emas yoki buzilgan",
        )

    # Fallback to DB only for draft versions (e.g. during creator preview simulation)
    rounds = (
        db.query(Round)
        .options(
            selectinload(Round.round_questions)
            .joinedload(RoundQuestion.question)
            .selectinload(Question.accepted_answers)
        )
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )
    result = []
    for r in rounds:
        rqs = sorted(r.round_questions, key=lambda x: x.sequence)
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
                primary_ans = next((a.answer_text for a in q.accepted_answers if a.is_primary), None)
                alt_ans = [a.answer_text for a in q.accepted_answers if not a.is_primary]
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
                    "primary_answer": primary_ans,
                    "accepted_answers": accepted,
                    "all_accepted_answers": accepted,
                    "config_override": rq.config_override or {},
                })
        else:
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
                primary_ans = next((a.answer_text for a in q.accepted_answers if a.is_primary), None)
                alt_ans = [a.answer_text for a in q.accepted_answers if not a.is_primary]
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
                    "primary_answer": primary_ans,
                    "accepted_answers": accepted,
                    "all_accepted_answers": accepted,
                    "config_override": {},
                })
        result.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "name": (r.config or {}).get("name", f"{r.sequence}-tur" if r.round_type == "zakovat_classic" else f"{r.sequence}-raund"),
            "description": (r.config or {}).get("description", ""),
            "config": r.config or {},
            "questions": questions_data,
        })
    return result


def calculate_question_time_limit(question_text: str, round_type: str = "zakovat_classic", timer_enabled: bool = True) -> int:
    """
    Deterministic calculation of authentic Zakovat question timer.
    Returns 0 if timer is disabled.

    Authentic Zakovat Principle:
    In live Zakovat matches, the 60-second thinking countdown begins ONLY AFTER
    the question has been read aloud. In digital format, reading the question
    takes player time before reasoning can begin.

    Formula:
    - Base thinking time: 90 seconds (generous authentic Zakovat thinking window).
    - Reading allowance: calculated at ~2.5 words per second (~150 words/min)
      for cognitive comprehension of intricate trivia setup.
      Formula: max(0, int(round(len(words) / 2.5)))
    - Minimum bound: 90 seconds.
    - Maximum bound: 150 seconds (2 minutes 30 seconds).

    For fast-paced modes (e.g., blitz):
      Base = 20s, Bounds = 20s to 45s.
    """
    if not timer_enabled:
        return 0

    words = (question_text or "").split()
    word_count = len(words)

    if round_type == "blitz":
        base_time = 20
        reading_allowance = max(0, int(round(word_count / 3.0)))
        total = base_time + reading_allowance
        return max(20, min(total, 45))

    # Zakovat / Standard / Other modes
    base_time = 90
    reading_allowance = max(0, int(round(word_count / 2.5)))
    total = base_time + reading_allowance
    return max(90, min(total, 150))


def format_question_response(
    q_data: Dict[str, Any],
    question_index: int,
    total_in_round: int,
    round_data: Dict[str, Any],
    attempt: Optional[SoloAttempt] = None,
) -> dict:
    """Formats safe question payload for client display (answers never exposed)."""
    # Sanitize options for MCQ (only key and text)
    raw_options = q_data.get("options")
    safe_options = None
    if raw_options and isinstance(raw_options, list):
        safe_options = [
            {"key": opt.get("key"), "text": opt.get("text")}
            for opt in raw_options
            if isinstance(opt, dict) and "key" in opt and "text" in opt
        ]

    round_type = round_data.get("round_type", "standard")

    # Determine if timer is enabled for this attempt
    is_timed = True
    if attempt:
        if attempt.timer_mode in ("no_timer", "preview_no_timer"):
            is_timed = False
        elif (attempt.attempt_metadata or {}).get("timer_enabled") is False:
            is_timed = False

    if not is_timed:
        time_limit = 0
    else:
        time_limit = calculate_question_time_limit(q_data.get("text", ""), round_type, timer_enabled=True)
        # Check if round config has an explicit override
        round_cfg_limit = round_data.get("config", {}).get("time_limit_seconds")
        if round_cfg_limit and int(round_cfg_limit) > 0:
            time_limit = int(round_cfg_limit)

    remaining_seconds = time_limit
    if is_timed and time_limit > 0 and attempt and attempt.question_opened_at:
        now = datetime.now(timezone.utc)
        opened_at = attempt.question_opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        elapsed = int((now - opened_at).total_seconds())
        remaining_seconds = max(0, time_limit - elapsed)

    r_seq = round_data.get("sequence", 1)
    if round_type in ("classic_zakovat", "zakovat_classic"):
        round_title = f"{r_seq}-tur"
    else:
        round_title = round_data.get("config", {}).get("title") or round_data.get("round_type", f"Raund {r_seq}")

    return {
        "status": "in_progress",
        "round": {
            "id": round_data.get("round_id"),
            "sequence": round_data.get("sequence"),
            "name": round_title,
            "round_type": round_data.get("round_type"),
            "config": sanitize_config(round_data.get("config", {})),
        },
        "question": {
            "id": q_data.get("question_id"),
            "question_id": q_data.get("question_id"),
            "round_question_id": q_data.get("round_question_id"),
            "sequence": q_data.get("sequence"),
            "text": q_data.get("text"),
            "media_provider": q_data.get("media_provider"),
            "media_url": q_data.get("media_url"),
            "question_type": q_data.get("question_type", "text"),
            "options": safe_options,
            "points": q_data.get("points", 1),
            "time_limit_seconds": time_limit,
            "remaining_time_seconds": remaining_seconds,
        },
        "round_question_index": question_index,
        "round_total_questions": total_in_round,
    }


# =========================================================
# GAMEPLAY ENDPOINTS
# =========================================================

@router.post("/start/{quiz_id}", status_code=status.HTTP_201_CREATED)
def start_solo_attempt(
    quiz_id: int,
    request: Request,
    response: Response,
    payload: Optional[StartPlayRequest] = None,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

    latest_published = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not latest_published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published version available")

    rounds = get_manifest_or_db_rounds(latest_published, db)
    if not rounds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quiz has no rounds")

    first_round = rounds[0]
    questions = first_round.get("questions", [])
    if not questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="First round has no questions")

    # Determine registered user vs anonymous visitor
    user_id = current_user.id if current_user else None
    anon_id = None
    set_anon_cookie = False

    if not user_id:
        # Anonymous user: enforce exactly ONE attempt per visitor
        anon_id = get_authoritative_anon_id(request)
        if not anon_id:
            anon_id = "anon_" + secrets.token_urlsafe(24)
            set_anon_cookie = True

        # Check if an attempt was already started with this anon_id
        existing = db.query(SoloAttempt).filter(SoloAttempt.anon_id == anon_id).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bepul urinishdan foydalanildi. O'ynashni davom ettirish uchun ro'yxatdan o'ting yoki tizimga kiring.",
            )

    timer_mode_req = (payload.timer_mode if payload and payload.timer_mode else "with_timer").lower()
    if timer_mode_req not in ("with_timer", "no_timer"):
        timer_mode_req = "with_timer"

    timer_mode_db = "standard" if timer_mode_req == "with_timer" else "no_timer"

    now = datetime.now(timezone.utc)
    attempt = SoloAttempt(
        quiz_version_id=latest_published.id,
        user_id=user_id,
        anon_id=anon_id if not user_id else None,
        timer_mode=timer_mode_db,
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        total_correct=0,
        active_time_seconds=0,
        question_opened_at=now,
        status="in_progress",
        attempt_metadata={
            "timer_mode": timer_mode_req,
            "timer_enabled": (timer_mode_req != "no_timer"),
        },
    )
    try:
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bepul urinishdan foydalanildi. O'ynashni davom ettirish uchun ro'yxatdan o'ting yoki tizimga kiring.",
        )

    if set_anon_cookie and not user_id:
        response.set_cookie(
            key="zakowhat_anon_id",
            value=anon_id,
            httponly=True,
            samesite="lax",
            secure=is_secure_cookie(),
            path="/",
            max_age=365 * 24 * 3600,
        )

    return {
        "session_token": attempt.session_token,
        "quiz_title": quiz.title,
        "game_mode": latest_published.game_mode,
        "timer_mode": timer_mode_req,
        "current_state": format_question_response(questions[0], 0, len(questions), first_round, attempt),
    }


def verify_staging_access(current_user: User = Depends(get_current_user_required)) -> User:
    """
    Verifies that the current user is authorized to access internal staging packs.
    Policy:
    - Requires an authenticated user session (401 if unauthenticated).
    - If ADMIN_EMAILS / STAGING_AUTHORIZED_EMAILS is configured in the environment,
      restricts access to matching emails (403 if not authorized).
    - In internal/development single-tenant stage (when no email list is configured),
      any authenticated user is granted access.
    """
    admin_emails_str = os.getenv("ADMIN_EMAILS", os.getenv("STAGING_AUTHORIZED_EMAILS", "")).strip()
    owner_emails_str = os.getenv("OWNER_EMAIL", os.getenv("OWNER_EMAILS", "")).strip()
    allowed = []
    if admin_emails_str:
        allowed.extend([e.strip().lower() for e in admin_emails_str.split(",") if e.strip()])
    if owner_emails_str:
        allowed.extend([e.strip().lower() for e in owner_emails_str.split(",") if e.strip()])
    env = (os.getenv("ENVIRONMENT") or "").strip().lower()
    if env == "production" and not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staging kirish huquqi sozlanmagan. Kirish taqiqlangan."
        )
    if allowed and current_user.email.lower() not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ushbu sahifaga kirish uchun administrator huquqi talab qilinadi."
        )
    return current_user


# =========================================================
# PRIVATE STAGING GAMEPLAY ENDPOINTS (OWNER/ADMIN TESTING)
# =========================================================

@router.get("/staging/quizzes", status_code=status.HTTP_200_OK)
def list_staging_quizzes(
    db: Session = Depends(get_db),
    current_user: User = Depends(verify_staging_access),
):
    """
    Private staging list of unreleased draft packs for owner/admin verification.
    Strictly isolated from public Arena discovery; requires authenticated session.
    """
    versions = (
        db.query(QuizVersion)
        .join(Quiz, QuizVersion.quiz_id == Quiz.id)
        .options(
            joinedload(QuizVersion.quiz),
            selectinload(QuizVersion.rounds).selectinload(Round.round_questions),
        )
        .filter(
            Quiz.title.like("Classic Zakovat — Pack %"),
            QuizVersion.status == "draft",
        )
        .order_by(Quiz.id.asc())
        .all()
    )
    results = []
    for v in versions:
        rq_count = sum(len(r.round_questions) for r in v.rounds)
        results.append({
            "quiz_id": v.quiz_id,
            "version_id": v.id,
            "version_number": v.version_number,
            "title": v.quiz.title,
            "description": v.quiz.description,
            "game_mode": v.game_mode,
            "status": v.status,
            "total_questions": rq_count,
            "total_rounds": len(v.rounds),
            "is_staging": True,
        })
    return results


@router.get("/staging/quizzes/{quiz_id}", status_code=status.HTTP_200_OK)
def get_staging_quiz_detail(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(verify_staging_access),
):
    """
    Returns the quiz structure for an unreleased draft pack in staging.
    Enables authorized staging users to preview round and question metadata in the player UI.
    """
    version = (
        db.query(QuizVersion)
        .join(Quiz, QuizVersion.quiz_id == Quiz.id)
        .options(
            joinedload(QuizVersion.quiz),
            selectinload(QuizVersion.rounds)
            .selectinload(Round.round_questions)
            .joinedload(RoundQuestion.question),
        )
        .filter(
            or_(QuizVersion.quiz_id == quiz_id, QuizVersion.id == quiz_id),
            QuizVersion.status == "draft",
        )
        .first()
    )
    if not version or not version.quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staging quiz draft not found")

    quiz = version.quiz
    rounds_data = []
    total_questions = 0
    for r in sorted(version.rounds, key=lambda x: x.sequence):
        sorted_rqs = sorted(r.round_questions, key=lambda x: x.sequence)
        total_questions += len(sorted_rqs)
        q_list = []
        for rq in sorted_rqs:
            q = rq.question
            q_list.append({
                "id": q.id,
                "round_question_id": rq.id,
                "sequence": rq.sequence,
                "text": q.text,
                "question_type": q.question_type or "text",
            })
        r_name = f"{r.sequence}-tur" if (version.game_mode == "classic_zakovat" or r.round_type == "zakovat_classic") else f"{r.sequence}-raund"
        rounds_data.append({
            "id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "name": (r.config or {}).get("name") or r_name,
            "questions": q_list,
        })

    return {
        "quiz_id": quiz.id,
        "version_id": version.id,
        "version_number": version.version_number,
        "title": quiz.title,
        "description": quiz.description,
        "game_mode": version.game_mode,
        "category": "Zakovat",
        "total_rounds": len(rounds_data),
        "total_questions": total_questions,
        "estimated_duration_minutes": max(1, round(total_questions * 1.25)),
        "rounds": rounds_data,
        "is_staging": True,
        "leaderboard_preview": [],
        "top_player": None,
    }


@router.post("/staging/start/{quiz_id}", status_code=status.HTTP_201_CREATED)
def start_staging_attempt(
    quiz_id: int,
    request: Request,
    response: Response,
    payload: Optional[StartPlayRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(verify_staging_access),
):
    """
    Private staging play endpoint for owner/admin to test staged draft packs as a real player.
    Reuses existing SoloAttempt gameplay pipeline with timer_mode='preview'.
    Strictly isolated from public leaderboards and anonymous attempt limits.
    """
    # Accept either quiz_id or draft/version_id
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    version = None
    if quiz:
        version = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "draft")
            .first()
        )
    else:
        version = db.query(QuizVersion).filter(QuizVersion.id == quiz_id, QuizVersion.status == "draft").first()
        if version:
            quiz = version.quiz

    if not quiz or not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staging qoralamasi topilmadi")

    rounds = get_manifest_or_db_rounds(version, db)
    if not rounds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kvizda raundlar yo'q")

    first_round = rounds[0]
    questions = first_round.get("questions", [])
    if not questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Birinchi raundda savollar yo'q")

    timer_mode_req = (payload.timer_mode if payload and payload.timer_mode else "with_timer").lower()
    if timer_mode_req not in ("with_timer", "no_timer"):
        timer_mode_req = "with_timer"

    timer_mode_db = "preview" if timer_mode_req == "with_timer" else "preview_no_timer"

    now = datetime.now(timezone.utc)
    attempt = SoloAttempt(
        quiz_version_id=version.id,
        user_id=current_user.id if current_user else None,
        anon_id=None,  # No anon quota consumption in staging mode
        timer_mode=timer_mode_db,  # Isolated session: excluded from leaderboards
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        total_correct=0,
        active_time_seconds=0,
        question_opened_at=now,
        status="in_progress",
        attempt_metadata={
            "timer_mode": timer_mode_req,
            "timer_enabled": (timer_mode_req != "no_timer"),
            "is_staging": True,
        },
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return {
        "session_token": attempt.session_token,
        "quiz_title": quiz.title,
        "game_mode": version.game_mode,
        "timer_mode": timer_mode_req,
        "is_staging": True,
        "current_state": format_question_response(questions[0], 0, len(questions), first_round, attempt),
    }


@router.get("/{session_token}", status_code=status.HTTP_200_OK)
def get_current_state(session_token: str, db: Session = Depends(get_db)):
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    quiz_id = attempt.quiz_version.quiz_id if attempt.quiz_version else None
    is_staging = bool((attempt.attempt_metadata or {}).get("is_staging"))

    if attempt.status == "completed":
        return {
            "status": "completed",
            "total_score": attempt.total_score,
            "quiz_id": quiz_id,
            "is_staging": is_staging,
        }

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    current_round = rounds[attempt.current_round_index]
    questions = current_round.get("questions", [])

    if attempt.status == "intermediate_break":
        q_ids = [q.get("question_id") for q in questions if q.get("question_id")]
        q_records = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.question_id.in_(q_ids))
            .all()
        )
        correct_count = sum(1 for r in q_records if r.is_correct)
        unanswered_count = sum(1 for r in q_records if (r.record_metadata or {}).get("is_blank"))
        incorrect_count = max(0, len(q_records) - correct_count - unanswered_count)
        meta = attempt.attempt_metadata or {}
        break_duration = meta.get("break_duration_seconds", 180)
        break_started_at = meta.get("break_started_at")
        tur_num = attempt.current_round_index + 1

        return {
            "status": "intermediate_break",
            "message": f"{tur_num}-tur yakunlandi. 2-turga o'tish uchun '2-turga o'tish' tugmasini bosing.",
            "completed_round_index": attempt.current_round_index,
            "total_score": attempt.total_score,
            "quiz_id": quiz_id,
            "is_staging": is_staging,
            "break_data": {
                "round_title": f"{tur_num}-TUR YAKUNLANDI",
                "message": f"{tur_num}-tur yakunlandi. 2-turga o'tish uchun tayyor bo'lganingizda '2-turga o'tish' tugmasini bosing.",
                "completed_questions": len(questions),
                "total_questions": sum(len(r.get("questions", [])) for r in rounds),
                "correct_count": correct_count,
                "incorrect_count": incorrect_count,
                "unanswered_count": unanswered_count,
                "break_duration_seconds": break_duration,
                "break_started_at": break_started_at,
            },
        }

    if attempt.status == "round_reveal":
        return {
            "status": "round_reveal",
            "message": "Round completed. Call /reveal to view answers and /continue to proceed.",
            "completed_round_index": attempt.current_round_index,
            "total_score": attempt.total_score,
            "quiz_id": quiz_id,
            "is_staging": is_staging,
        }

    current_q = questions[attempt.current_question_index]
    resp = format_question_response(current_q, attempt.current_question_index, len(questions), current_round, attempt)
    resp["total_score"] = attempt.total_score
    resp["quiz_id"] = quiz_id
    resp["is_staging"] = is_staging
    resp["current_state"] = dict(resp)
    return resp


@router.post("/{session_token}/answer", status_code=status.HTTP_200_OK)
def submit_answer(
    session_token: str,
    submission: AnswerSubmission,
    request: Request,
    db: Session = Depends(get_db)
):
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)

    # 0. Idempotency Key check across all states
    idempotency_key = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Request-Id")
        or submission.client_submission_id
    )
    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        existing_records = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id)
            .order_by(AnswerRecord.id.desc())
            .all()
        )
        for rec in existing_records:
            rec_meta = rec.record_metadata or {}
            if rec_meta.get("idempotency_key") == idempotency_key:
                current_round = rounds[attempt.current_round_index] if attempt.current_round_index < len(rounds) else None
                questions = current_round.get("questions", []) if current_round else []
                current_q = questions[attempt.current_question_index] if (current_round and attempt.current_question_index < len(questions)) else None
                return {
                    "is_correct": rec.is_correct,
                    "points_awarded": rec.points_awarded,
                    "total_score": attempt.total_score,
                    "round_completed": False,
                    "quiz_completed": attempt.status == "completed",
                    "reveal_available": False,
                    "is_duplicate": True,
                    "next_state": format_question_response(
                        current_q, attempt.current_question_index, len(questions), current_round, attempt
                    ) if (attempt.status == "in_progress" and current_q) else None,
                }

    # 1. Idempotency handling when attempt is already completed
    if attempt.status == "completed":
        last_rec = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id)
            .order_by(AnswerRecord.id.desc())
            .first()
        )
        is_id_match = False
        if submission.question_id is not None or submission.round_question_id is not None:
            if last_rec and (
                (submission.question_id is not None and last_rec.question_id == submission.question_id)
                or (submission.round_question_id is not None and last_rec.round_question_id == submission.round_question_id)
            ):
                is_id_match = True
        elif last_rec and submission.answer.strip() == last_rec.submitted_text.strip():
            is_id_match = True

        if is_id_match and last_rec:
            return {
                "is_correct": last_rec.is_correct,
                "points_awarded": last_rec.points_awarded,
                "total_score": attempt.total_score,
                "round_completed": True,
                "quiz_completed": True,
                "reveal_available": True,
                "next_state": None,
                "is_duplicate": True,
            }
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attempt is already completed")

    # 2. Idempotency handling when attempt is in break or reveal
    if attempt.status in ("round_reveal", "intermediate_break"):
        last_rec = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id)
            .order_by(AnswerRecord.id.desc())
            .first()
        )
        is_id_match = False
        if submission.question_id is not None or submission.round_question_id is not None:
            if last_rec and (
                (submission.question_id is not None and last_rec.question_id == submission.question_id)
                or (submission.round_question_id is not None and last_rec.round_question_id == submission.round_question_id)
            ):
                is_id_match = True
        elif attempt.status == "intermediate_break" and last_rec and submission.answer.strip() == last_rec.submitted_text.strip():
            is_id_match = True

        if is_id_match and last_rec:
            if attempt.status == "intermediate_break":
                meta = attempt.attempt_metadata or {}
                break_duration = meta.get("break_duration_seconds", 180)
                break_started_at = meta.get("break_started_at")
                tur_num = attempt.current_round_index + 1
                q_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
                correct_count = sum(1 for r in q_records if r.is_correct)
                unanswered_count = sum(1 for r in q_records if (r.record_metadata or {}).get("is_blank"))
                incorrect_count = max(0, len(q_records) - correct_count - unanswered_count)
                return {
                    "is_correct": last_rec.is_correct,
                    "points_awarded": last_rec.points_awarded,
                    "total_score": attempt.total_score,
                    "round_completed": False,
                    "intermediate_break": True,
                    "quiz_completed": False,
                    "reveal_available": False,
                    "is_duplicate": True,
                    "break_data": {
                        "round_title": f"{tur_num}-TUR YAKUNLANDI",
                        "message": f"{tur_num}-tur yakunlandi. 2-turga o'tish uchun tayyor bo'lganingizda '2-turga o'tish' tugmasini bosing.",
                        "completed_questions": len(q_records),
                        "total_questions": sum(len(r.get("questions", [])) for r in rounds),
                        "correct_count": correct_count,
                        "incorrect_count": incorrect_count,
                        "unanswered_count": unanswered_count,
                        "break_duration_seconds": break_duration,
                        "break_started_at": break_started_at,
                    },
                    "next_state": None,
                }
            else:
                is_last_round = attempt.current_round_index >= (len(rounds) - 1)
                return {
                    "is_correct": last_rec.is_correct,
                    "points_awarded": last_rec.points_awarded,
                    "total_score": attempt.total_score,
                    "round_completed": True,
                    "quiz_completed": is_last_round,
                    "reveal_available": True,
                    "next_state": None,
                    "is_duplicate": True,
                }
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current stage is finished. Call /continue to proceed to the next stage."
        )

    current_round = rounds[attempt.current_round_index]
    questions = current_round.get("questions", [])
    current_q = questions[attempt.current_question_index]
    current_qid = current_q.get("question_id")
    current_rqid = current_q.get("round_question_id")

    # 3. Guard against retries or question mismatches while in_progress
    if submission.question_id is not None or submission.round_question_id is not None:
        matches_current = True
        if submission.question_id is not None and submission.question_id != current_qid:
            matches_current = False
        if submission.round_question_id is not None and submission.round_question_id != current_rqid:
            matches_current = False

        if not matches_current:
            # Check if this question was already answered earlier in this attempt (idempotent retry)
            query_filter = []
            if submission.question_id is not None:
                query_filter.append(AnswerRecord.question_id == submission.question_id)
            if submission.round_question_id is not None:
                query_filter.append(AnswerRecord.round_question_id == submission.round_question_id)

            existing_record = (
                db.query(AnswerRecord)
                .filter(AnswerRecord.attempt_id == attempt.id, or_(*query_filter))
                .order_by(AnswerRecord.id.desc())
                .first()
            )
            if existing_record:
                # Return idempotent response for already recorded answer; do NOT advance question index
                return {
                    "is_correct": existing_record.is_correct,
                    "points_awarded": existing_record.points_awarded,
                    "total_score": attempt.total_score,
                    "round_completed": False,
                    "quiz_completed": False,
                    "reveal_available": False,
                    "is_duplicate": True,
                    "next_state": format_question_response(
                        current_q, attempt.current_question_index, len(questions), current_round, attempt
                    ),
                }

            # Question ID is not current and was not answered in this attempt:
            # Client must NEVER advance state or skip questions!
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Question mismatch: submitted question ID does not match current attempt question."
            )
    # When question_id and round_question_id are both omitted:
    # Server-side current question state remains strictly authoritative.
    # Never use answer-text equality alone as proof of duplicate identity.
    # A legitimate wrong (or correct) answer on current_q is evaluated for current_q and advances normally.

    raw_answer = submission.answer or ""
    trimmed_answer = raw_answer.strip()
    round_type = current_round.get("round_type", "standard")
    game_mode = getattr(version, "game_mode", "modern_multiround")

    # Determine wager intent: explicit flag or leading '+'
    is_wager = submission.is_wager
    if trimmed_answer.startswith("+"):
        is_wager = True
        trimmed_answer = trimmed_answer[1:].strip()

    # Detect blank/pass submission
    is_blank = trimmed_answer == "" or trimmed_answer.lower() in ("pass", "o'tkazish", "otkazish")

    accepted_answers_data = current_q.get("accepted_answers", [])
    q_points = current_q.get("points", 1) or 1
    cleaned_input = normalize_uzbek_latin(trimmed_answer)

    is_correct = False
    matched_answer_text = None

    if not is_blank:
        # Check MCQ (Gulmisiz, rayhonmisiz?)
        options = current_q.get("options")
        is_mcq = (round_type in ("gulmisiz", "gulmisiz_rayhonmisiz", "mcq")) or (current_q.get("question_type") == "mcq")

        if is_mcq and options and isinstance(options, list):
            # 1. Player submitted option key (A, B, C, D)
            matching_opt = next((opt for opt in options if opt.get("key", "").upper() == trimmed_answer.upper()), None)
            if matching_opt:
                # Compare against accepted answers (key or text)
                for ans in accepted_answers_data:
                    norm_ans = normalize_uzbek_latin(ans.get("answer_text", ""))
                    if norm_ans in (normalize_uzbek_latin(matching_opt["key"]), normalize_uzbek_latin(matching_opt["text"])):
                        is_correct = True
                        matched_answer_text = matching_opt["key"]
                        break
            else:
                # 2. Player submitted option text directly
                for ans in accepted_answers_data:
                    norm_ans = normalize_uzbek_latin(ans.get("answer_text", ""))
                    if norm_ans == cleaned_input:
                        is_correct = True
                        matched_answer_text = norm_ans
                        break

        # Check True/False (Aldama meni)
        elif round_type in ("true_false", "aldama_meni"):
            for ans in accepted_answers_data:
                norm_ans = normalize_uzbek_latin(ans.get("answer_text", ""))
                if is_true_false_match(cleaned_input, norm_ans):
                    is_correct = True
                    matched_answer_text = norm_ans
                    break

        # Standard text evaluation
        else:
            for ans in accepted_answers_data:
                norm_ans = normalize_uzbek_latin(ans.get("answer_text", ""))
                if norm_ans == cleaned_input:
                    is_correct = True
                    matched_answer_text = norm_ans
                    break

        # Zanjir chain rule: answer must start with the terminal letter of the previous primary answer
        if is_correct and round_type == "zanjir" and attempt.current_question_index > 0:
            prev_q = questions[attempt.current_question_index - 1]
            prev_accepted_list = prev_q.get("accepted_answers", [])
            prev_primary = next((a["answer_text"] for a in prev_accepted_list if a.get("is_primary")), None)
            if not prev_primary and prev_accepted_list:
                prev_primary = prev_accepted_list[0].get("answer_text")

            if prev_primary:
                if not verify_zanjir_chain(prev_primary, matched_answer_text or cleaned_input):
                    is_correct = False

    # =========================================================
    # SCORING COMPUTATION
    # =========================================================
    points = 0

    if round_type == "vabank":
        if is_blank:
            points = 0
            is_correct = False
        else:
            if is_wager:
                points = 2 if is_correct else -2
            else:
                points = 1 if is_correct else -1

    elif game_mode == "svoyak" or round_type == "svoyak_theme":
        val = q_points
        if is_blank:
            points = 0
            is_correct = False
        else:
            points = val if is_correct else -val

    else:
        # Default / Modern standard / Classic Zakovat (1 pt per correct, 0 on wrong/blank)
        points = q_points if is_correct else 0

    # =========================================================
    # AUDIT LOG (AnswerRecord) & ACTIVE TIMING
    # =========================================================
    q_id = current_q.get("question_id")
    rq_id = current_q.get("round_question_id")

    now = datetime.now(timezone.utc)
    time_taken = 0
    if attempt.question_opened_at:
        q_opened = attempt.question_opened_at
        if q_opened.tzinfo is None:
            q_opened = q_opened.replace(tzinfo=timezone.utc)
        delta = max(0.0, (now - q_opened).total_seconds())

        # Determine if timer is enabled for this attempt
        is_timed = True
        if attempt.timer_mode in ("no_timer", "preview_no_timer"):
            is_timed = False
        elif (attempt.attempt_metadata or {}).get("timer_enabled") is False:
            is_timed = False

        if is_timed:
            # Authoritative question time limit cap
            round_cfg_limit = current_round.get("config", {}).get("time_limit_seconds")
            if round_cfg_limit and float(round_cfg_limit) > 0:
                limit_val = float(round_cfg_limit)
            else:
                limit_val = float(calculate_question_time_limit(current_q.get("text", ""), round_type))

            delta = min(delta, limit_val)

        time_taken = int(round(delta))
        attempt.active_time_seconds += time_taken

    meta = {"time_taken_seconds": time_taken}
    if is_wager:
        meta["is_wager"] = True
    if is_blank:
        meta["is_blank"] = True
    if idempotency_key:
        meta["idempotency_key"] = idempotency_key

    record = AnswerRecord(
        attempt_id=attempt.id,
        question_id=q_id,
        round_question_id=rq_id,
        submitted_text=raw_answer,
        is_correct=is_correct,
        points_awarded=points,
        answered_at=now,
        record_metadata=meta,
    )
    db.add(record)
    attempt.total_score += points
    attempt.current_question_index += 1

    # Check if current round completed
    if attempt.current_question_index >= len(questions):
        is_last_round = attempt.current_round_index >= (len(rounds) - 1)

        # In Classic Zakovat, between 1-tur and 2-tur is an intermediate break (not round reveal)
        if (game_mode == "classic_zakovat" or round_type == "zakovat_classic") and not is_last_round:
            attempt.status = "intermediate_break"
            attempt.question_opened_at = None  # Pauses active timer during break
            now_iso = datetime.now(timezone.utc).isoformat()
            meta = dict(attempt.attempt_metadata or {})
            meta["break_started_at"] = now_iso
            meta["break_duration_seconds"] = 180
            attempt.attempt_metadata = meta
            db.commit()

            q_records = (
                db.query(AnswerRecord)
                .filter(AnswerRecord.attempt_id == attempt.id)
                .all()
            )
            correct_count = sum(1 for r in q_records if r.is_correct)
            unanswered_count = sum(1 for r in q_records if (r.record_metadata or {}).get("is_blank"))
            incorrect_count = max(0, len(q_records) - correct_count - unanswered_count)
            tur_num = attempt.current_round_index + 1

            return {
                "is_correct": is_correct,
                "points_awarded": points,
                "total_score": attempt.total_score,
                "round_completed": False,
                "intermediate_break": True,
                "quiz_completed": False,
                "reveal_available": False,
                "break_data": {
                    "round_title": f"{tur_num}-TUR YAKUNLANDI",
                    "message": f"{tur_num}-tur yakunlandi. 2-turga o'tish uchun tayyor bo'lganingizda '2-turga o'tish' tugmasini bosing.",
                    "completed_questions": len(q_records),
                    "total_questions": sum(len(r.get("questions", [])) for r in rounds),
                    "correct_count": correct_count,
                    "incorrect_count": incorrect_count,
                    "unanswered_count": unanswered_count,
                    "break_duration_seconds": 180,
                    "break_started_at": now_iso,
                },
                "next_state": None,
            }

        attempt.status = "round_reveal"
        attempt.question_opened_at = None  # Pauses active timer during reveal
        db.commit()

        return {
            "is_correct": is_correct,
            "points_awarded": points,
            "total_score": attempt.total_score,
            "round_completed": True,
            "quiz_completed": is_last_round,
            "reveal_available": True,
            "next_state": None,
        }

    attempt.question_opened_at = datetime.now(timezone.utc)
    db.commit()
    next_q = questions[attempt.current_question_index]
    return {
        "is_correct": is_correct,
        "points_awarded": points,
        "total_score": attempt.total_score,
        "round_completed": False,
        "quiz_completed": False,
        "reveal_available": False,
        "next_state": format_question_response(next_q, attempt.current_question_index, len(questions), current_round, attempt),
    }


@router.get("/{session_token}/reveal", status_code=status.HTTP_200_OK)
def get_round_reveal(session_token: str, db: Session = Depends(get_db)):
    """Exposes answers and rules ONLY for the round currently completed and waiting for reveal."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status not in ("round_reveal", "completed", "intermediate_break"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reveal is not available until the round is fully answered."
        )

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    is_last_round = attempt.current_round_index >= (len(rounds) - 1)
    target_round = rounds[attempt.current_round_index]

    if is_last_round:
        # Post-game final review: include ALL 24 questions across ALL rounds
        all_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
        record_map = {r.question_id: r for r in all_records}

        review_rounds = []
        all_revealed_questions = []

        for r in rounds:
            round_questions = []
            for q in r.get("questions", []):
                qid = q.get("question_id")
                rec = record_map.get(qid)
                correct_answers = [a.get("answer_text") for a in q.get("accepted_answers", [])]
                primary_ans = next((a.get("answer_text") for a in q.get("accepted_answers", []) if a.get("is_primary")), None)
                if not primary_ans and correct_answers:
                    primary_ans = correct_answers[0]

                is_blank = (rec.record_metadata or {}).get("is_blank", False) if rec else True
                is_unanswered = is_blank or (rec is None) or (rec.submitted_text == "" or rec.submitted_text is None)
                points = rec.points_awarded if rec else 0

                q_dict = {
                    "question_id": qid,
                    "round_question_id": q.get("round_question_id"),
                    "sequence": q.get("sequence"),
                    "text": q.get("text"),
                    "explanation": q.get("explanation"),
                    "options": q.get("options"),
                    "submitted_answer": rec.submitted_text if rec and not is_blank else None,
                    "is_correct": rec.is_correct if rec else False,
                    "is_unanswered": is_unanswered,
                    "points_awarded": points,
                    "primary_answer": primary_ans,
                    "correct_answers": correct_answers,
                }
                round_questions.append(q_dict)
                all_revealed_questions.append(q_dict)

            r_seq = r.get("sequence", 1)
            r_type = r.get("round_type", "")
            game_mode = getattr(attempt.quiz_version, "game_mode", "modern_multiround")
            if game_mode == "classic_zakovat" or r_type == "zakovat_classic":
                default_title = f"{r_seq}-tur"
            else:
                default_title = f"{r_seq}-raund"
            r_title = (r.get("config") or {}).get("name") or default_title

            review_rounds.append({
                "round_id": r.get("round_id"),
                "round_sequence": r_seq,
                "round_type": r_type,
                "round_title": r_title,
                "config": r.get("config", {}),
                "questions": round_questions,
            })

        total_correct = sum(1 for q in all_revealed_questions if q.get("is_correct"))
        total_unanswered = sum(1 for q in all_revealed_questions if q.get("is_unanswered"))
        total_incorrect = len(all_revealed_questions) - total_correct - total_unanswered

        target_round_questions = next(
            (r["questions"] for r in review_rounds if r["round_id"] == target_round.get("round_id")),
            []
        )
        target_round_score = sum(q.get("points_awarded", 0) for q in target_round_questions)

        return {
            "round_id": target_round.get("round_id"),
            "round_sequence": target_round.get("sequence"),
            "round_type": target_round.get("round_type"),
            "round_score": target_round_score,
            "total_score_so_far": attempt.total_score,
            "total_correct": total_correct,
            "total_incorrect": total_incorrect,
            "total_unanswered": total_unanswered,
            "total_questions": len(all_revealed_questions),
            "config": target_round.get("config", {}),
            "rounds": review_rounds,
            "questions": all_revealed_questions,
            "has_next_round": False,
            "is_final_round": True,
        }

    # Per-round reveal for intermediate rounds
    questions = target_round.get("questions", [])
    question_ids = [q.get("question_id") for q in questions if q.get("question_id")]
    records = {
        rec.question_id: rec
        for rec in db.query(AnswerRecord)
        .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.question_id.in_(question_ids))
        .all()
    }

    revealed_questions = []
    round_score = 0

    for q in questions:
        qid = q.get("question_id")
        rec = records.get(qid)
        correct_answers = [a.get("answer_text") for a in q.get("accepted_answers", [])]
        points = rec.points_awarded if rec else 0
        round_score += points

        revealed_questions.append({
            "question_id": qid,
            "round_question_id": q.get("round_question_id"),
            "sequence": q.get("sequence"),
            "text": q.get("text"),
            "explanation": q.get("explanation"),
            "options": q.get("options"),
            "submitted_answer": rec.submitted_text if rec else None,
            "is_correct": rec.is_correct if rec else False,
            "is_unanswered": (rec is None) or (rec.submitted_text == "" or rec.submitted_text is None),
            "points_awarded": points,
            "correct_answers": correct_answers,
        })

    return {
        "round_id": target_round.get("round_id"),
        "round_sequence": target_round.get("sequence"),
        "round_type": target_round.get("round_type"),
        "round_score": round_score,
        "total_score_so_far": attempt.total_score,
        "config": target_round.get("config", {}),
        "questions": revealed_questions,
        "has_next_round": not is_last_round,
        "is_final_round": False,
    }


@router.post("/{session_token}/continue", status_code=status.HTTP_200_OK)
def continue_to_next_round(session_token: str, db: Session = Depends(get_db)):
    """Advances from intermediate_break or round_reveal to the next stage, or returns completed status idempotently."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status == "completed":
        mins = attempt.active_time_seconds // 60
        secs = attempt.active_time_seconds % 60
        return {
            "status": "completed",
            "quiz_completed": True,
            "total_score": attempt.total_score,
            "total_correct": attempt.total_correct,
            "active_time_seconds": attempt.active_time_seconds,
            "formatted_time": f"{mins:02d}:{secs:02d}",
            "message": "Quiz completed! Fetch /results for summary.",
        }

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    current_round = rounds[attempt.current_round_index]
    questions = current_round.get("questions", [])

    # Resumption from intermediate break to next round
    if attempt.status == "intermediate_break":
        attempt.current_round_index += 1
        attempt.current_question_index = 0
        attempt.status = "in_progress"
        attempt.question_opened_at = datetime.now(timezone.utc)  # Starts Q13 (2-tur Q1) timer fresh
        db.commit()

        next_round = rounds[attempt.current_round_index]
        next_questions = next_round.get("questions", [])
        next_q = next_questions[0]
        return {
            "status": "in_progress",
            "quiz_completed": False,
            "current_state": format_question_response(next_q, 0, len(next_questions), next_round, attempt),
        }

    if attempt.status != "round_reveal":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is not currently in a round reveal or break state."
        )

    is_last_round = attempt.current_round_index >= (len(rounds) - 1)

    if is_last_round:
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        attempt.question_opened_at = None
        # Authoritative server-side calculation of total_correct
        attempt.total_correct = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.is_correct == True)
            .count()
        )
        db.commit()
        mins = attempt.active_time_seconds // 60
        secs = attempt.active_time_seconds % 60
        return {
            "status": "completed",
            "quiz_completed": True,
            "total_score": attempt.total_score,
            "total_correct": attempt.total_correct,
            "active_time_seconds": attempt.active_time_seconds,
            "formatted_time": f"{mins:02d}:{secs:02d}",
            "message": "Quiz completed! Fetch /results for summary.",
        }

    attempt.current_round_index += 1
    attempt.current_question_index = 0
    attempt.status = "in_progress"
    attempt.question_opened_at = datetime.now(timezone.utc)  # Resumes active question timer!
    db.commit()

    next_round = rounds[attempt.current_round_index]
    next_questions = next_round.get("questions", [])

    return {
        "status": "in_progress",
        "quiz_completed": False,
        "current_state": format_question_response(next_questions[0], 0, len(next_questions), next_round, attempt),
    }


@router.get("/{session_token}/results", status_code=status.HTTP_200_OK)
def get_final_results(session_token: str, db: Session = Depends(get_db)):
    """Returns the end-of-quiz score breakdown once the attempt is completed."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)
    is_last_round = attempt.current_round_index >= (len(rounds) - 1)

    total_q_count = sum(len(r.get("questions", [])) for r in rounds)
    ans_count = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).count()

    # Resilient auto-finalization: If called during round_reveal on last round,
    # or if all questions across all rounds have been answered,
    # finalize attempt automatically so the user never gets HTTP 400.
    if attempt.status != "completed" and (
        (attempt.status == "round_reveal" and is_last_round)
        or (total_q_count > 0 and ans_count >= total_q_count)
    ):
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        attempt.question_opened_at = None
        attempt.total_correct = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.is_correct == True)
            .count()
        )
        db.commit()

    if attempt.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Quiz results are only available after completing the attempt."
        )

    all_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    record_map = {r.question_id: r for r in all_records}

    round_summaries = []
    total_correct = 0
    total_incorrect = 0
    total_unanswered = 0
    game_mode = getattr(version, "game_mode", "modern_multiround")

    for r in rounds:
        questions = r.get("questions", [])
        r_score = 0
        r_correct = 0
        r_incorrect = 0
        r_unanswered = 0

        for q in questions:
            qid = q.get("question_id")
            rec = record_map.get(qid)
            if rec and rec.is_correct:
                r_correct += 1
                r_score += rec.points_awarded
            elif rec and (rec.record_metadata or {}).get("is_blank"):
                r_unanswered += 1
            elif rec:
                r_incorrect += 1
                r_score += rec.points_awarded
            else:
                r_unanswered += 1

        total_correct += r_correct
        total_incorrect += r_incorrect
        total_unanswered += r_unanswered

        r_seq = r.get("sequence", 1)
        r_type = r.get("round_type", "")
        if game_mode == "classic_zakovat" or r_type == "zakovat_classic":
            default_title = f"{r_seq}-tur"
        else:
            default_title = f"{r_seq}-raund"
        r_title = (r.get("config") or {}).get("name") or default_title

        round_summaries.append({
            "round_id": r.get("round_id"),
            "round_sequence": r_seq,
            "round_type": r_type,
            "round_title": r_title,
            "round_score": r_score,
            "correct_count": r_correct,
            "incorrect_count": r_incorrect,
            "unanswered_count": r_unanswered,
        })

    mins = attempt.active_time_seconds // 60
    secs = attempt.active_time_seconds % 60

    return {
        "status": "completed",
        "is_preview": (attempt.timer_mode == "preview"),
        "is_anonymous": (attempt.user_id is None),
        "game_mode": getattr(version, "game_mode", "modern_multiround"),
        "total_score": attempt.total_score,
        "total_correct": attempt.total_correct if attempt.total_correct is not None else total_correct,
        "total_incorrect": total_incorrect,
        "total_unanswered": total_unanswered,
        "total_questions": total_correct + total_incorrect + total_unanswered,
        "active_time_seconds": attempt.active_time_seconds,
        "formatted_time": f"{mins:02d}:{secs:02d}",
        "started_at": attempt.started_at.isoformat(),
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
        "rounds": round_summaries,
    }


@router.get("/{session_token}/review", status_code=status.HTTP_200_OK)
def get_answer_review(session_token: str, db: Session = Depends(get_db)):
    """
    Returns full question-by-question review with explanations and answers
    ONLY for completed attempts.
    For Mantiqqasqon, the hidden_rule is safely revealed here since the quiz is completed.
    """
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status not in ("completed", "round_reveal"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Javoblar tahlili faqat viktorina to'liq yakunlangandan keyin ko'rsatiladi."
        )

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)
    all_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    record_map = {r.question_id: r for r in all_records}

    review_rounds = []
    total_correct = 0
    total_incorrect = 0
    total_unanswered = 0

    for r in rounds:
        questions_review = []
        for q in r.get("questions", []):
            qid = q.get("question_id")
            rec = record_map.get(qid)
            correct_answers = [a.get("answer_text") for a in q.get("accepted_answers", [])]
            primary_ans = next((a.get("answer_text") for a in q.get("accepted_answers", []) if a.get("is_primary")), None)
            if not primary_ans and correct_answers:
                primary_ans = correct_answers[0]

            is_blank = (rec.record_metadata or {}).get("is_blank", False) if rec else True
            is_unanswered = is_blank or (rec is None) or (rec.submitted_text == "" or rec.submitted_text is None)

            if rec and rec.is_correct:
                total_correct += 1
            elif is_unanswered:
                total_unanswered += 1
            else:
                total_incorrect += 1

            questions_review.append({
                "question_id": qid,
                "round_question_id": q.get("round_question_id"),
                "sequence": q.get("sequence"),
                "text": q.get("text"),
                "question_type": q.get("question_type", "text"),
                "options": q.get("options"),
                "explanation": q.get("explanation"),
                "submitted_answer": rec.submitted_text if rec and not is_blank else None,
                "is_correct": rec.is_correct if rec else False,
                "is_unanswered": is_unanswered,
                "points_awarded": rec.points_awarded if rec else 0,
                "time_taken_seconds": (rec.record_metadata or {}).get("time_taken_seconds", 0) if rec else 0,
                "primary_answer": primary_ans,
                "correct_answers": correct_answers,
            })

        r_seq = r.get("sequence", 1)
        r_type = r.get("round_type", "")
        game_mode = getattr(version, "game_mode", "modern_multiround")
        if game_mode == "classic_zakovat" or r_type == "zakovat_classic":
            default_title = f"{r_seq}-tur"
        else:
            default_title = f"{r_seq}-raund"
        r_title = (r.get("config") or {}).get("name") or default_title

        review_rounds.append({
            "round_id": r.get("round_id"),
            "round_sequence": r_seq,
            "round_type": r_type,
            "round_title": r_title,
            "config": r.get("config", {}),  # Safely reveals hidden_rule now that attempt is complete
            "questions": questions_review,
        })

    mins = attempt.active_time_seconds // 60
    secs = attempt.active_time_seconds % 60

    return {
        "status": "completed",
        "quiz_id": version.quiz_id,
        "version_number": version.version_number,
        "total_score": attempt.total_score,
        "total_correct": attempt.total_correct if attempt.total_correct is not None else total_correct,
        "total_incorrect": total_incorrect,
        "total_unanswered": total_unanswered,
        "total_questions": total_correct + total_incorrect + total_unanswered,
        "active_time_seconds": attempt.active_time_seconds,
        "formatted_time": f"{mins:02d}:{secs:02d}",
        "rounds": review_rounds,
    }