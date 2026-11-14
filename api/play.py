import os
import string
import secrets
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict, Tuple
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, AnswerRecord, User
from services.gameplay import normalize_uzbek_latin, verify_zanjir_chain, is_true_false_match, is_conservative_typo_match
from api.auth import get_current_user_optional, get_current_user_required, is_secure_cookie, get_authoritative_anon_id, get_owner_emails

router = APIRouter(prefix="/api/play", tags=["play"])

SESSION_INACTIVITY_LIMIT_SECONDS = 1800  # 30 minutes server-side inactivity limit


def get_client_ip(request: Request) -> str:
    """Extracts client IP from proxy headers (e.g. Render, Cloudflare) or socket host."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def enforce_session_not_expired(attempt: SoloAttempt, db: Session) -> None:
    """
    Enforces server-side session expiry after SESSION_INACTIVITY_LIMIT_SECONDS (30 minutes) of inactivity.
    If expired:
    - Sets attempt.status = 'expired'
    - Prevents resumption, submitting answers, or continuing
    - Raises HTTP 410 Gone
    """
    if attempt.status == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="O'yin sessiyasi muddati tugagan (30 daqiqa faolsizlik). Yangi o'yin boshlang."
        )

    if attempt.status == "abandoned":
        # Ended by an explicit restart/new start; it can never be resumed or answered again
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Bu o'yin qaytadan boshlangan. Yangi sessiyadan foydalaning."
        )

    if attempt.status == "completed":
        return

    now = datetime.now(timezone.utc)
    meta = attempt.attempt_metadata or {}
    last_active_str = meta.get("last_active_at")
    last_active = None
    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str)
        except Exception:
            last_active = None

    if not last_active:
        last_rec = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id)
            .order_by(AnswerRecord.id.desc())
            .first()
        )
        if last_rec and last_rec.answered_at:
            last_active = last_rec.answered_at
        elif attempt.question_opened_at:
            last_active = attempt.question_opened_at
        elif attempt.started_at:
            last_active = attempt.started_at

    if last_active:
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        elapsed = (now - last_active).total_seconds()
        if elapsed > SESSION_INACTIVITY_LIMIT_SECONDS:
            attempt.status = "expired"
            updated_meta = dict(attempt.attempt_metadata or {})
            updated_meta["expired_at"] = now.isoformat()
            attempt.attempt_metadata = updated_meta
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="O'yin sessiyasi muddati tugagan (30 daqiqa faolsizlik). Yangi o'yin boshlang."
            )

    # Active session: touch activity timestamp
    updated_meta = dict(attempt.attempt_metadata or {})
    updated_meta["last_active_at"] = now.isoformat()
    attempt.attempt_metadata = updated_meta


def enforce_attempt_ownership(attempt: SoloAttempt, current_user: Optional[User]) -> None:
    """
    Enforces user isolation for player attempts.
    If the attempt belongs to a registered user:
    - Unauthenticated requests are rejected with 401.
    - Requests from a different user are rejected with 403 Forbidden.
    - Staging/creator preview sessions remain accessible by owner/admins.
    """
    if attempt.user_id is not None:
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Tizimga kirish talab qilinadi",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if current_user.id != attempt.user_id:
            meta = attempt.attempt_metadata or {}
            is_preview = str(attempt.timer_mode).startswith("preview") or bool(meta.get("is_staging"))
            # Owner demonstrations belong to the owner who started them: nobody else, staff included.
            if meta.get("is_demo"):
                is_preview = False
            staff_emails = set(get_owner_emails()) | {
                e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
            }
            is_admin_or_owner = (current_user.email or "").lower() in staff_emails
            if not (is_preview and is_admin_or_owner):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Ruxsat etilmagan: bu boshqa foydalanuvchining o'yin sessiyasi",
                )



class AnswerSubmission(BaseModel):
    answer: str
    is_wager: bool = False
    question_id: Optional[int] = None
    round_question_id: Optional[int] = None
    client_submission_id: Optional[str] = None


class StartPlayRequest(BaseModel):
    timer_mode: Optional[str] = "with_timer"
    restart: Optional[bool] = False
    svoyak_mode: Optional[str] = "free_choice"
    play_mode: Optional[str] = None


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
    - Maximum bound: 180 seconds (3 minutes).

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

    if round_type == "svoyak_theme":
        base_time = 30
        reading_allowance = max(0, int(round(word_count / 3.0)))
        total = base_time + reading_allowance
        return max(30, min(total, 60))

    # Zakovat / Standard / Other modes
    base_time = 90
    reading_allowance = max(0, int(round(word_count / 2.5)))
    total = base_time + reading_allowance
    return max(90, min(total, 180))


_MISSING_EXPLANATION_MARKERS = {"", "none", "null", "nan", "-"}


def clean_explanation(value: Any) -> Optional[str]:
    """Returns a genuine explanation string, or None when the data has no real explanation.
    Explanations are optional: imported data may hold None, '' or the literal string 'None'."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _MISSING_EXPLANATION_MARKERS:
        return None
    return text


def resolve_primary_answer(q: Dict[str, Any]) -> Optional[str]:
    """Primary correct answer of a manifest question (falls back to the first accepted answer)."""
    primary = q.get("primary_answer")
    if primary:
        return primary
    accepted = q.get("accepted_answers") or []
    primary = next((a.get("answer_text") for a in accepted if a.get("is_primary")), None)
    if not primary and accepted:
        primary = accepted[0].get("answer_text")
    return primary


def build_answer_reveal(q: Dict[str, Any], submitted_text: Optional[str], is_blank: bool) -> Dict[str, Any]:
    """Per-question reveal payload: correct answer and the player's answer always; explanation only if genuine."""
    return {
        "correct_answer": resolve_primary_answer(q),
        "explanation": clean_explanation(q.get("explanation")),
        "submitted_answer": None if is_blank else (submitted_text or None),
    }


# Per-question reveal pacing: after each classic Zakovat / Classic Svoyak answer the next question is
# withheld (and its clock stopped) until the player presses "Davom etish" (POST /continue).
# Setting this to False restores the previous behaviour (next_state returned, clock started immediately).
PER_QUESTION_REVEAL_PACING_ENABLED = True


def is_reveal_paced(attempt: SoloAttempt, game_mode: Optional[str], round_type: Optional[str]) -> bool:
    """Modes where each answer is followed by a reveal and the next question opens only on /continue."""
    if not PER_QUESTION_REVEAL_PACING_ENABLED:
        return False
    if game_mode == "classic_zakovat" or round_type == "zakovat_classic":
        return True
    if game_mode == "svoyak" and (attempt.attempt_metadata or {}).get("svoyak_mode") == "classic":
        return True
    return False


def is_awaiting_continue(attempt: SoloAttempt) -> bool:
    return attempt.status == "in_progress" and bool((attempt.attempt_metadata or {}).get("awaiting_continue"))


def set_awaiting_continue(attempt: SoloAttempt, value: bool) -> None:
    meta = dict(attempt.attempt_metadata or {})
    if value:
        meta["awaiting_continue"] = True
    else:
        meta.pop("awaiting_continue", None)
    attempt.attempt_metadata = meta


def build_record_reveal(record: Optional[AnswerRecord], rounds: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Rebuilds the reveal for an already stored answer (used for replays and page refresh)."""
    if record is None:
        return None
    q = next(
        (q for r in rounds for q in r.get("questions", []) if (q.get("question_id") or q.get("id")) == record.question_id),
        None,
    )
    if q is None:
        return None
    rec_meta = record.record_metadata or {}
    is_blank = bool(rec_meta.get("is_blank"))
    reveal = build_answer_reveal(q, record.submitted_text, is_blank)
    reveal.update({
        "question_text": q.get("text"),
        "is_correct": record.is_correct,
        "points_awarded": record.points_awarded,
        "timed_out": bool(rec_meta.get("timed_out")),
        "is_passed": is_blank,
    })
    return reveal


# =====================================================================
# RANKED (COMPETITIVE) ATTEMPT RULE — one ranked opportunity per user per pack
# - The first started TIMED public attempt on a pack (any version) is the ranked attempt.
# - No-timer play is practice and only unlocks after that timed attempt has been started.
# - Restarting / starting again never resets or recreates the ranked opportunity:
#   an active ranked attempt is marked "abandoned" and a new practice attempt is created.
# - Decided at attempt creation and stored in attempt_metadata["competitive"] (no migration).
#   Attempts created before this rule have no such key and keep their legacy leaderboard treatment.
# =====================================================================
TIMED_PUBLIC_TIMER_MODES = ("standard", "competitive")
ACTIVE_ATTEMPT_STATUSES = ("in_progress", "board_selection", "intermediate_break", "round_reveal")
NO_TIMER_LOCKED_DETAIL = (
    "Vaqtsiz rejim bu to'plam uchun avval vaqt bilan boshlangan reyting o'yinidan so'ng ochiladi."
)


def demo_correct_adjustment(attempt: SoloAttempt) -> int:
    """Owner demonstrations (api/demo.py) may carry a manual score adjustment; Classic counts correct
    answers, so the adjustment is added to that count. Always 0 for every other attempt."""
    meta = attempt.attempt_metadata or {}
    if not meta.get("is_demo") or (attempt.quiz_version and attempt.quiz_version.game_mode == "svoyak"):
        return 0
    return int(meta.get("demo_score_adjustment", 0))


def is_preview_attempt(attempt: SoloAttempt) -> bool:
    return str(attempt.timer_mode or "").startswith("preview") or bool((attempt.attempt_metadata or {}).get("is_staging"))


def pack_version_ids(db: Session, quiz_id: int) -> List[int]:
    return [vid for (vid,) in db.query(QuizVersion.id).filter(QuizVersion.quiz_id == quiz_id).all()]


# Attempts that use up the ranked opportunity: timed public attempts and staff preview attempts
# (a preview reveals the pack's answers, so it must not be followed by a ranked run after publication).
RANKED_OPPORTUNITY_CONSUMING_MODES = TIMED_PUBLIC_TIMER_MODES + ("preview", "preview_no_timer")


def lock_user_for_attempt_creation(db: Session, user_id: int) -> None:
    """Serializes attempt creation per user at the DATABASE level.
    A no-op UPDATE on the user's row takes a row lock in PostgreSQL (and the database write lock in
    SQLite) that is held until this request's transaction commits. A concurrent start/restart for the
    same user blocks here until the first one has committed, so its existence checks then see that
    attempt: two simultaneous requests can never both create a ranked attempt."""
    db.execute(text("UPDATE users SET display_name = display_name WHERE id = :uid"), {"uid": user_id})


def has_started_timed_attempt(db: Session, user_id: int, version_ids: List[int]) -> bool:
    """True if the user already used the pack's ranked opportunity: any timed public attempt or any
    preview attempt on this pack (any version, any status, legacy included)."""
    if not version_ids:
        return False
    return db.query(SoloAttempt.id).filter(
        SoloAttempt.user_id == user_id,
        SoloAttempt.quiz_version_id.in_(version_ids),
        SoloAttempt.timer_mode.in_(RANKED_OPPORTUNITY_CONSUMING_MODES),
    ).first() is not None


def attempt_mode_fields(attempt: SoloAttempt) -> Dict[str, Any]:
    """The attempt's REAL timer mode and ranked status, as stored on the server (never the requested values)."""
    return {
        "timer_mode": "no_timer" if attempt.timer_mode in ("no_timer", "preview_no_timer") else "with_timer",
        "ranked": is_ranked_or_legacy_timed(attempt),
    }


def is_ranked_or_legacy_timed(attempt: SoloAttempt) -> bool:
    """Attempts that can produce a leaderboard result: new ranked attempts and pre-rule (legacy) timed attempts."""
    if is_preview_attempt(attempt) or attempt.timer_mode not in TIMED_PUBLIC_TIMER_MODES:
        return False
    return (attempt.attempt_metadata or {}).get("competitive", True) is not False


def abandon_active_ranked_attempts(db: Session, user_id: int, version_ids: List[int], now: datetime) -> None:
    """Starting another attempt on a pack ends any still-active ranked/legacy timed attempt for it,
    so it can never be finished later with answers learned in the other attempt."""
    if not version_ids:
        return
    active = db.query(SoloAttempt).filter(
        SoloAttempt.user_id == user_id,
        SoloAttempt.quiz_version_id.in_(version_ids),
        SoloAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
    ).all()
    for att in active:
        if is_ranked_or_legacy_timed(att):
            att.status = "abandoned"
            att.question_opened_at = None
            meta = dict(att.attempt_metadata or {})
            meta["abandoned_at"] = now.isoformat()
            att.attempt_metadata = meta


def build_svoyak_board(attempt: SoloAttempt, rounds: List[Dict[str, Any]], db: Session) -> Dict[str, Any]:
    """Constructs the full interactive board state for Svoya Igra."""
    records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    answered_map = {r.question_id: r for r in records}

    themes = []
    total_questions = 0
    total_played = 0

    for r_idx, r in enumerate(rounds):
        theme_title = (r.get("config") or {}).get("theme_title") or r.get("name") or f"{r_idx + 1}-Mavzu"
        cells = []
        for q_idx, q in enumerate(r.get("questions", [])):
            total_questions += 1
            qid = q.get("question_id") or q.get("id")
            rec = answered_map.get(qid)
            is_played = rec is not None
            if is_played:
                total_played += 1
            cells.append({
                "round_index": r_idx,
                "question_index": q_idx,
                "question_id": qid,
                "round_question_id": q.get("round_question_id"),
                "sequence": q.get("sequence", q_idx + 1),
                "points": q.get("points", (q_idx + 1) * 10),
                "is_played": is_played,
                "is_correct": rec.is_correct if rec else None,
                "points_awarded": rec.points_awarded if rec else None,
                "is_passed": bool((rec.record_metadata or {}).get("is_blank")) if rec else False,
            })
        themes.append({
            "round_index": r_idx,
            "round_id": r.get("round_id") or r.get("id"),
            "theme_title": theme_title,
            "cells": cells,
        })

    return {
        "themes": themes,
        "total_questions": total_questions,
        "total_played": total_played,
        "total_score": attempt.total_score,
        "total_correct": attempt.total_correct,
        "all_completed": (total_played >= total_questions and total_questions > 0),
    }


def get_svoyak_classic_next_question(
    attempt: SoloAttempt, rounds: List[Dict[str, Any]], db: Session
) -> Optional[Tuple[int, int, Dict[str, Any]]]:
    """Finds the next sequential unplayed question for Classic Svoyak mode.
    Sequence: Category 0 (10->50), Category 1 (10->50), ..., Category N (10->50).
    """
    records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    answered_qids = {r.question_id for r in records}

    for r_idx, r in enumerate(rounds):
        questions = r.get("questions", [])
        for q_idx, q in enumerate(questions):
            qid = q.get("question_id") or q.get("id")
            if qid not in answered_qids:
                return (r_idx, q_idx, q)
    return None


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
    timer_paused = False
    if is_timed and time_limit > 0 and attempt and attempt.question_opened_at:
        now = datetime.now(timezone.utc)
        paused_at = (attempt.attempt_metadata or {}).get("demo_paused_at")
        if paused_at:
            timer_paused = True
            now = datetime.fromisoformat(paused_at)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        opened_at = attempt.question_opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        elapsed = int((now - opened_at).total_seconds())
        remaining_seconds = max(0, time_limit - elapsed)

    r_seq = round_data.get("sequence", 1)
    if round_type in ("classic_zakovat", "zakovat_classic"):
        round_title = f"{r_seq}-tur"
    elif round_type == "svoyak_theme":
        round_title = round_data.get("config", {}).get("theme_title") or round_data.get("name") or f"{r_seq}-mavzu"
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
            "timer_paused": timer_paused,
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
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="O'yinni boshlash uchun ro'yxatdan o'ting yoki tizimga kiring",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = current_user.id

    timer_mode_req = (payload.timer_mode if payload and payload.timer_mode else "with_timer").lower()
    if timer_mode_req not in ("with_timer", "no_timer"):
        timer_mode_req = "with_timer"

    timer_mode_db = "standard" if timer_mode_req == "with_timer" else "no_timer"

    now = datetime.now(timezone.utc)

    # Serialize concurrent starts for this user (DB row lock held until commit) BEFORE the resume check,
    # so a simultaneous second request resumes the first one's attempt instead of creating another.
    lock_user_for_attempt_creation(db, user_id)

    # Check for existing active, resumable attempt for this user and quiz version (avoids duplicate attempts)
    restart_requested = bool(payload and getattr(payload, "restart", False))
    if not restart_requested:
        existing_attempt = (
            db.query(SoloAttempt)
            .filter(
                SoloAttempt.user_id == user_id,
                SoloAttempt.quiz_version_id == latest_published.id,
                # Canonical active statuses (incl. round_reveal): reopening a pack from the Arena during a
                # round reveal must resume that attempt, never fall through to "new attempt" (which would
                # abandon the ranked attempt).
                SoloAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                # Owner demonstrations (preview class) are separate sessions and never resumed here.
                SoloAttempt.timer_mode.notin_(("preview", "preview_no_timer")),
            )
            .order_by(SoloAttempt.id.desc())
            .first()
        )
        if existing_attempt:
            # Check if active session is expired (> 300s of inactivity)
            meta = existing_attempt.attempt_metadata or {}
            last_active_str = meta.get("last_active_at")
            is_expired = False
            if last_active_str:
                try:
                    last_active = datetime.fromisoformat(last_active_str)
                    if last_active.tzinfo is None:
                        last_active = last_active.replace(tzinfo=timezone.utc)
                    if (now - last_active).total_seconds() > SESSION_INACTIVITY_LIMIT_SECONDS:
                        is_expired = True
                except Exception:
                    pass
            elif existing_attempt.started_at:
                st = existing_attempt.started_at
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                if (now - st).total_seconds() > SESSION_INACTIVITY_LIMIT_SECONDS:
                    is_expired = True

            if is_expired:
                existing_attempt.status = "expired"
                db.commit()
            else:
                # Refresh last active timestamp and return existing session (prevents duplicate row)
                meta["last_active_at"] = now.isoformat()
                existing_attempt.attempt_metadata = meta
                db.commit()
                db.refresh(existing_attempt)

                # Paused stages (round reveal, intermediate break, per-question reveal pause) are resumed
                # WITHOUT a question payload: the client restores them from GET /{token}. This keeps the same
                # attempt and never exposes a withheld next question through /start.
                if existing_attempt.status in ("round_reveal", "intermediate_break") or is_awaiting_continue(existing_attempt):
                    return {
                        "session_token": existing_attempt.session_token,
                        "quiz_title": quiz.title,
                        "quiz_id": quiz.id,
                        "game_mode": latest_published.game_mode,
                        "status": existing_attempt.status,
                        "awaiting_continue": is_awaiting_continue(existing_attempt),
                        "total_score": existing_attempt.total_score,
                        "current_state": None,
                        "resumed": True,
                        "restore_required": True,
                        **attempt_mode_fields(existing_attempt),
                    }

                cur_r_idx = existing_attempt.current_round_index or 0
                cur_q_idx = existing_attempt.current_question_index or 0
                active_round = rounds[cur_r_idx] if cur_r_idx < len(rounds) else first_round
                active_questions = active_round.get("questions", [])
                active_q = active_questions[cur_q_idx] if cur_q_idx < len(active_questions) else questions[0]

                if latest_published.game_mode == "svoyak":
                    board_data = build_svoyak_board(existing_attempt, rounds, db)
                    cur_svoyak_mode = (existing_attempt.attempt_metadata or {}).get("svoyak_mode", "free_choice")
                    
                    if cur_svoyak_mode == "classic":
                        next_item = get_svoyak_classic_next_question(existing_attempt, rounds, db)
                        if next_item:
                            cur_r_idx, cur_q_idx, active_q = next_item
                            existing_attempt.current_round_index = cur_r_idx
                            existing_attempt.current_question_index = cur_q_idx
                            if existing_attempt.question_opened_at is None:
                                existing_attempt.question_opened_at = now
                            existing_attempt.status = "in_progress"
                            db.commit()
                            active_round = rounds[cur_r_idx]
                            return {
                                "session_token": existing_attempt.session_token,
                                "quiz_title": quiz.title,
                                "game_mode": latest_published.game_mode,
                                "svoyak_mode": "classic",
                                "timer_mode": timer_mode_req,
                                "status": "in_progress",
                                "board": board_data,
                                "total_score": existing_attempt.total_score,
                                "current_state": format_question_response(active_q, cur_q_idx, len(active_round.get("questions", [])), active_round, existing_attempt),
                                "resumed": True,
                                **attempt_mode_fields(existing_attempt),  # real mode, not the requested one
                            }

                    return {
                        "session_token": existing_attempt.session_token,
                        "quiz_title": quiz.title,
                        "game_mode": latest_published.game_mode,
                        "svoyak_mode": cur_svoyak_mode,
                        "timer_mode": timer_mode_req,
                        "status": "board_selection" if existing_attempt.question_opened_at is None else existing_attempt.status,
                        "board": board_data,
                        "total_score": existing_attempt.total_score,
                        "current_state": format_question_response(active_q, cur_q_idx, len(active_questions), active_round, existing_attempt) if existing_attempt.question_opened_at is not None else None,
                        "resumed": True,
                        **attempt_mode_fields(existing_attempt),  # real mode, not the requested one
                    }

                return {
                    "session_token": existing_attempt.session_token,
                    "quiz_title": quiz.title,
                    "game_mode": latest_published.game_mode,
                    "timer_mode": timer_mode_req,
                    "current_state": format_question_response(active_q, cur_q_idx, len(active_questions), active_round, existing_attempt),
                    "resumed": True,
                    **attempt_mode_fields(existing_attempt),  # real mode, not the requested one
                }

    req_svoyak_mode = "free_choice"
    if payload:
        if payload.svoyak_mode in ("classic", "classic_sequential"):
            req_svoyak_mode = "classic"
        elif payload.play_mode in ("classic", "classic_sequential"):
            req_svoyak_mode = "classic"

    # Ranked-attempt rule (server-side): one ranked opportunity per user per pack.
    # Re-take the per-user lock (an expired-attempt commit above would have released it) so the
    # checks below and the insert run atomically with respect to concurrent requests.
    lock_user_for_attempt_creation(db, user_id)
    version_ids = pack_version_ids(db, quiz.id)
    prior_timed = has_started_timed_attempt(db, user_id, version_ids)
    is_timed_request = timer_mode_db in TIMED_PUBLIC_TIMER_MODES
    if not is_timed_request and not prior_timed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_TIMER_LOCKED_DETAIL)
    # A new attempt (e.g. restart=true) ends any still-active ranked attempt on this pack
    abandon_active_ranked_attempts(db, user_id, version_ids, now)

    attempt_meta = {
        "timer_mode": timer_mode_req,
        "timer_enabled": (timer_mode_req != "no_timer"),
        "svoyak_mode": req_svoyak_mode,
        "last_active_at": now.isoformat(),
        "competitive": bool(is_timed_request and not prior_timed),
    }

    attempt = SoloAttempt(
        quiz_version_id=latest_published.id,
        user_id=user_id,
        anon_id=None,
        timer_mode=timer_mode_db,
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        total_correct=0,
        active_time_seconds=0,
        question_opened_at=now,
        status="in_progress",
        attempt_metadata=attempt_meta,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    if latest_published.game_mode == "svoyak":
        board_data = build_svoyak_board(attempt, rounds, db)
        if req_svoyak_mode == "classic":
            first_q = rounds[0]["questions"][0]
            attempt.current_round_index = 0
            attempt.current_question_index = 0
            attempt.question_opened_at = now
            attempt.status = "in_progress"
            db.commit()
            return {
                "session_token": attempt.session_token,
                "quiz_title": quiz.title,
                "game_mode": latest_published.game_mode,
                "svoyak_mode": "classic",
                "timer_mode": timer_mode_req,
                "status": "in_progress",
                "board": board_data,
                "total_score": 0,
                "current_state": format_question_response(first_q, 0, len(rounds[0].get("questions", [])), rounds[0], attempt),
                **attempt_mode_fields(attempt),
            }

        attempt.question_opened_at = None
        attempt.status = "board_selection"
        db.commit()
        return {
            "session_token": attempt.session_token,
            "quiz_title": quiz.title,
            "game_mode": latest_published.game_mode,
            "svoyak_mode": "free_choice",
            "timer_mode": timer_mode_req,
            "status": "board_selection",
            "board": board_data,
            "total_score": 0,
            "current_state": None,
            **attempt_mode_fields(attempt),
        }

    return {
        "session_token": attempt.session_token,
        "quiz_title": quiz.title,
        "game_mode": latest_published.game_mode,
        "timer_mode": timer_mode_req,
        "current_state": format_question_response(questions[0], 0, len(questions), first_round, attempt),
        **attempt_mode_fields(attempt),
    }


def verify_staging_access(current_user: User = Depends(get_current_user_required)) -> User:
    """
    Verifies that the current user is authorized to access internal staging packs.
    Policy:
    - Requires an authenticated user session (401 if unauthenticated).
    - Requires an explicit allowlist in EVERY environment: ADMIN_EMAILS / STAGING_AUTHORIZED_EMAILS
      and/or OWNER_EMAIL(S). Only matching emails are allowed (403 otherwise).
    - Fails closed: with no allowlist configured, nobody can access staging (drafts would otherwise
      leak unpublished questions and answers to ordinary users before publication).
    """
    admin_emails_str = os.getenv("ADMIN_EMAILS", os.getenv("STAGING_AUTHORIZED_EMAILS", "")).strip()
    owner_emails_str = os.getenv("OWNER_EMAIL", os.getenv("OWNER_EMAILS", "")).strip()
    allowed = []
    if admin_emails_str:
        allowed.extend([e.strip().lower() for e in admin_emails_str.split(",") if e.strip()])
    if owner_emails_str:
        allowed.extend([e.strip().lower() for e in owner_emails_str.split(",") if e.strip()])
    if not allowed:
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
    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.id == quiz_id, QuizVersion.status == "draft")
        .first()
    )
    if not version:
        version = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "draft")
            .order_by(QuizVersion.version_number.desc())
            .first()
        )
    quiz = version.quiz if version else None

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

    req_svoyak_mode = "free_choice"
    if payload:
        if payload.svoyak_mode in ("classic", "classic_sequential"):
            req_svoyak_mode = "classic"
        elif payload.play_mode in ("classic", "classic_sequential"):
            req_svoyak_mode = "classic"

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
            "svoyak_mode": req_svoyak_mode,
            "is_staging": True,
            "last_active_at": now.isoformat(),
        },
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    if version.game_mode == "svoyak":
        board_data = build_svoyak_board(attempt, rounds, db)
        if req_svoyak_mode == "classic":
            first_q = rounds[0]["questions"][0]
            attempt.current_round_index = 0
            attempt.current_question_index = 0
            attempt.question_opened_at = now
            attempt.status = "in_progress"
            db.commit()
            return {
                "session_token": attempt.session_token,
                "quiz_title": quiz.title,
                "game_mode": "svoyak",
                "svoyak_mode": "classic",
                "timer_mode": timer_mode_req,
                "is_staging": True,
                "status": "in_progress",
                "board": board_data,
                "total_score": 0,
                "current_state": format_question_response(first_q, 0, len(rounds[0].get("questions", [])), rounds[0], attempt),
            }

        attempt.question_opened_at = None
        attempt.status = "board_selection"
        db.commit()
        return {
            "session_token": attempt.session_token,
            "quiz_title": quiz.title,
            "game_mode": "svoyak",
            "svoyak_mode": "free_choice",
            "timer_mode": timer_mode_req,
            "is_staging": True,
            "status": "board_selection",
            "board": board_data,
            "total_score": 0,
            "current_state": None,
        }

    return {
        "session_token": attempt.session_token,
        "quiz_title": quiz.title,
        "game_mode": version.game_mode,
        "timer_mode": timer_mode_req,
        "is_staging": True,
        "current_state": format_question_response(questions[0], 0, len(questions), first_round, attempt),
    }


@router.get("/{session_token}", status_code=status.HTTP_200_OK)
def get_current_state(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_session_not_expired(attempt, db)
    enforce_attempt_ownership(attempt, current_user)

    quiz_id = attempt.quiz_version.quiz_id if attempt.quiz_version else None
    is_staging = bool(
        (attempt.attempt_metadata or {}).get("is_staging")
        or str(attempt.timer_mode).startswith("preview")
    )

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)

    if attempt.status == "completed":
        res = {
            "status": "completed",
            "total_score": attempt.total_score,
            "quiz_id": quiz_id,
            "is_staging": is_staging,
        }
        if attempt.quiz_version and attempt.quiz_version.game_mode == "svoyak":
            res["board"] = build_svoyak_board(attempt, rounds, db)
            res["game_mode"] = "svoyak"
        return res

    # Answer reveal pending: never expose the next question until the player presses "Davom etish" (/continue)
    if is_awaiting_continue(attempt):
        last_rec = (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id)
            .order_by(AnswerRecord.id.desc())
            .first()
        )
        game_mode = attempt.quiz_version.game_mode if attempt.quiz_version else None
        res = {
            "status": "in_progress",
            "awaiting_continue": True,
            "game_mode": game_mode,
            "total_score": attempt.total_score,
            "quiz_id": quiz_id,
            "is_staging": is_staging,
            "last_reveal": build_record_reveal(last_rec, rounds),
            "current_state": None,
            **attempt_mode_fields(attempt),
        }
        if game_mode == "svoyak":
            res["svoyak_mode"] = (attempt.attempt_metadata or {}).get("svoyak_mode", "free_choice")
            res["board"] = build_svoyak_board(attempt, rounds, db)
        return res

    if attempt.quiz_version and attempt.quiz_version.game_mode == "svoyak":
        cur_svoyak_mode = (attempt.attempt_metadata or {}).get("svoyak_mode", "free_choice")
        board_data = build_svoyak_board(attempt, rounds, db)
        
        if cur_svoyak_mode == "classic":
            next_item = get_svoyak_classic_next_question(attempt, rounds, db)
            if next_item:
                r_idx, q_idx, q_obj = next_item
                attempt.current_round_index = r_idx
                attempt.current_question_index = q_idx
                if attempt.question_opened_at is None:
                    attempt.question_opened_at = datetime.now(timezone.utc)
                attempt.status = "in_progress"
                db.commit()
                r_obj = rounds[r_idx]
                return {
                    "status": "in_progress",
                    "game_mode": "svoyak",
                    "svoyak_mode": "classic",
                    "total_score": attempt.total_score,
                    "quiz_id": quiz_id,
                    "is_staging": is_staging,
                    "board": board_data,
                    "current_state": format_question_response(q_obj, q_idx, len(r_obj.get("questions", [])), r_obj, attempt),
                    **attempt_mode_fields(attempt),
                }

        if attempt.question_opened_at is None:
            return {
                "status": "board_selection",
                "game_mode": "svoyak",
                "svoyak_mode": cur_svoyak_mode,
                "total_score": attempt.total_score,
                "quiz_id": quiz_id,
                "is_staging": is_staging,
                "board": board_data,
                **attempt_mode_fields(attempt),
            }

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
        is_last_round = attempt.current_round_index >= (len(rounds) - 1)
        return {
            "status": "round_reveal",
            "message": "Round completed. Call /reveal to view answers and /continue to proceed.",
            "completed_round_index": attempt.current_round_index,
            "total_score": attempt.total_score,
            "quiz_id": quiz_id,
            "is_staging": is_staging,
            "is_final_round": is_last_round,
            "has_next_round": not is_last_round,
            "quiz_completed": is_last_round,
        }

    current_q = questions[attempt.current_question_index]
    resp = format_question_response(current_q, attempt.current_question_index, len(questions), current_round, attempt)
    resp["total_score"] = attempt.total_score
    resp["quiz_id"] = quiz_id
    resp["is_staging"] = is_staging
    resp.update(attempt_mode_fields(attempt))  # real timer mode + ranked status for page restore
    resp["current_state"] = dict(resp)
    if attempt.quiz_version and attempt.quiz_version.game_mode == "svoyak":
        resp["board"] = build_svoyak_board(attempt, rounds, db)
    return resp


@router.post("/{session_token}/restart", status_code=status.HTTP_200_OK)
def restart_play_session(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Restarts the play session for the same quiz pack from the beginning.
    - Active ranked (or legacy timed) session: marked "abandoned" (record kept); a NEW practice attempt is created.
    - Completed / expired / abandoned session: record kept intact; a NEW attempt is created (practice once the
      ranked opportunity has been used).
    - Active practice (or staging preview) session: reset in place (score 0, answers deleted, progression reset).
    - Preserves timer mode, svoyak mode, and pack identity.
    - Returns clean initial state for the client to render immediately.
    """
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Restart is an authenticated-player action: require a logged-in user and an owned attempt.
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tizimga kirish talab qilinadi",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if attempt.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ruxsat etilmagan: bu boshqa foydalanuvchining o'yin sessiyasi",
        )
    enforce_attempt_ownership(attempt, current_user)

    # Serialize with concurrent starts/restarts for this user, then re-read the attempt's current state
    lock_user_for_attempt_creation(db, attempt.user_id)
    db.refresh(attempt)

    now = datetime.now(timezone.utc)
    quiz_version = attempt.quiz_version
    if not quiz_version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz version not found")

    quiz = quiz_version.quiz
    game_mode = quiz_version.game_mode
    rounds = get_manifest_or_db_rounds(quiz_version, db)
    meta = dict(attempt.attempt_metadata or {})
    cur_svoyak_mode = meta.get("svoyak_mode", "free_choice")
    # DB timer modes: standard / no_timer (public), preview / preview_no_timer (staging).
    timer_mode = attempt.timer_mode or "standard"
    timer_mode_req = "no_timer" if timer_mode in ("no_timer", "preview_no_timer") else "with_timer"

    # Ranked-attempt rule: the ranked opportunity is never reset in place, and expired/abandoned
    # attempts are never revived. Those cases start a NEW attempt (practice); only active practice
    # (and staging preview) attempts are still reset in place.
    preview = is_preview_attempt(attempt)
    # Owner demonstrations are always restarted in place by the demo reset (same session, still a demo),
    # whatever their status; nothing below applies to them.
    is_demo_restart = preview and bool(meta.get("is_demo"))
    needs_new_attempt = not is_demo_restart and (attempt.status in ("completed", "expired", "abandoned") or (
        not preview and attempt.status in ACTIVE_ATTEMPT_STATUSES and is_ranked_or_legacy_timed(attempt)
    ))
    ranked_meta: Dict[str, Any] = {}
    if needs_new_attempt and not preview:
        version_ids = pack_version_ids(db, quiz_version.quiz_id)
        prior_timed = has_started_timed_attempt(db, attempt.user_id, version_ids)
        new_is_timed = timer_mode in TIMED_PUBLIC_TIMER_MODES
        if not new_is_timed and not prior_timed:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_TIMER_LOCKED_DETAIL)
        # Explicit restart consumes the ranked opportunity: the active ranked attempt becomes "abandoned"
        abandon_active_ranked_attempts(db, attempt.user_id, version_ids, now)
        ranked_meta["competitive"] = bool(new_is_timed and not prior_timed)

    if needs_new_attempt:
        new_token = secrets.token_urlsafe(32)
        new_attempt = SoloAttempt(
            session_token=new_token,
            user_id=attempt.user_id,
            quiz_version_id=quiz_version.id,
            total_score=0,
            total_correct=0,
            active_time_seconds=0,
            started_at=now,
            status="board_selection" if (game_mode == "svoyak" and cur_svoyak_mode == "free_choice") else "in_progress",
            current_round_index=0,
            current_question_index=0,
            question_opened_at=None if (game_mode == "svoyak" and cur_svoyak_mode == "free_choice") else now,
            timer_mode=timer_mode,
            attempt_metadata={
                "timer_mode": timer_mode_req,
                "timer_enabled": (timer_mode_req != "no_timer"),
                "svoyak_mode": cur_svoyak_mode,
                "last_active_at": now.isoformat(),
                "is_staging": meta.get("is_staging", False),
                **ranked_meta,
            },
        )
        db.add(new_attempt)
        db.commit()
        db.refresh(new_attempt)
        target_attempt = new_attempt
    elif is_demo_restart:
        from api.demo import reset_demo_in_place      # local import: api.demo imports this module
        reset_demo_in_place(attempt, rounds, db)
        db.commit()
        db.refresh(attempt)
        cur_svoyak_mode = "free_choice"               # demos always play Svoya Igra from the board
        target_attempt = attempt
    else:
        db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).delete()
        attempt.total_score = 0
        attempt.total_correct = 0
        attempt.active_time_seconds = 0
        attempt.current_round_index = 0
        attempt.current_question_index = 0
        attempt.started_at = now
        attempt.completed_at = None

        if game_mode == "svoyak":
            if cur_svoyak_mode == "classic":
                attempt.status = "in_progress"
                attempt.question_opened_at = now
            else:
                attempt.status = "board_selection"
                attempt.question_opened_at = None
        else:
            attempt.status = "in_progress"
            attempt.question_opened_at = now

        meta["last_active_at"] = now.isoformat()
        # Drop stale intermediate-break timer state from the previous run
        meta.pop("break_started_at", None)
        meta.pop("break_duration_seconds", None)
        meta.pop("awaiting_continue", None)
        attempt.attempt_metadata = meta
        db.commit()
        db.refresh(attempt)
        target_attempt = attempt

    is_staging = bool(
        (target_attempt.attempt_metadata or {}).get("is_staging")
        or str(target_attempt.timer_mode).startswith("preview")
    )
    quiz_id = quiz_version.quiz_id

    if game_mode == "svoyak":
        board_data = build_svoyak_board(target_attempt, rounds, db)
        if cur_svoyak_mode == "classic":
            first_round = rounds[0] if rounds else {}
            first_questions = first_round.get("questions", [])
            first_q = first_questions[0] if first_questions else {}
            return {
                "session_token": target_attempt.session_token,
                "quiz_title": quiz.title if quiz else "",
                "game_mode": "svoyak",
                "svoyak_mode": "classic",
                "timer_mode": timer_mode_req,
                "status": "in_progress",
                "board": board_data,
                "total_score": 0,
                "quiz_id": quiz_id,
                "is_staging": is_staging,
                "current_state": format_question_response(first_q, 0, len(first_questions), first_round, target_attempt),
                "restarted": True,
                **attempt_mode_fields(target_attempt),
            }
        else:
            return {
                "session_token": target_attempt.session_token,
                "quiz_title": quiz.title if quiz else "",
                "game_mode": "svoyak",
                "svoyak_mode": "free_choice",
                "timer_mode": timer_mode_req,
                "status": "board_selection",
                "board": board_data,
                "total_score": 0,
                "quiz_id": quiz_id,
                "is_staging": is_staging,
                "current_state": None,
                "restarted": True,
                **attempt_mode_fields(target_attempt),
            }

    first_round = rounds[0] if rounds else {}
    first_questions = first_round.get("questions", [])
    first_q = first_questions[0] if first_questions else {}
    return {
        "session_token": target_attempt.session_token,
        "quiz_title": quiz.title if quiz else "",
        "game_mode": game_mode,
        "timer_mode": timer_mode_req,
        "status": "in_progress",
        "total_score": 0,
        "quiz_id": quiz_id,
        "is_staging": is_staging,
        "current_state": format_question_response(first_q, 0, len(first_questions), first_round, target_attempt),
        "restarted": True,
        **attempt_mode_fields(target_attempt),
    }


class SelectBoardQuestionRequest(BaseModel):
    round_index: Optional[int] = None
    question_index: Optional[int] = None
    question_id: Optional[int] = None
    round_id: Optional[int] = None
    round_question_id: Optional[int] = None


@router.get("/{session_token}/board", status_code=status.HTTP_200_OK)
def get_play_board(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Retrieves the full interactive board state for Svoya Igra games."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_session_not_expired(attempt, db)
    enforce_attempt_ownership(attempt, current_user)
    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    board = build_svoyak_board(attempt, rounds, db)
    return {
        "session_token": session_token,
        "status": attempt.status,
        "game_mode": attempt.quiz_version.game_mode if attempt.quiz_version else "svoyak",
        "quiz_id": attempt.quiz_version.quiz_id if attempt.quiz_version else None,
        "quiz_title": attempt.quiz_version.quiz.title if attempt.quiz_version and attempt.quiz_version.quiz else None,
        "board": board,
    }


@router.post("/{session_token}/select", status_code=status.HTTP_200_OK)
def select_board_question(
    session_token: str,
    payload: SelectBoardQuestionRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Selects an unplayed question from the board in Svoya Igra."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_session_not_expired(attempt, db)
    enforce_attempt_ownership(attempt, current_user)

    if attempt.status == "completed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="O'yin allaqachon yakunlangan")

    # The next question opens only via /continue after a reveal (select must not bypass it)
    if is_awaiting_continue(attempt):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Keyingi savolni ochish uchun «Davom etish» tugmasini bosing.",
        )

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    target_r_idx = None
    target_q_idx = None

    # Locate target question
    if payload.question_id is not None:
        for r_i, r in enumerate(rounds):
            for q_i, q in enumerate(r.get("questions", [])):
                if q.get("question_id") == payload.question_id or q.get("id") == payload.question_id:
                    target_r_idx = r_i
                    target_q_idx = q_i
                    break
            if target_r_idx is not None:
                break
    elif payload.round_question_id is not None:
        for r_i, r in enumerate(rounds):
            for q_i, q in enumerate(r.get("questions", [])):
                if q.get("round_question_id") == payload.round_question_id:
                    target_r_idx = r_i
                    target_q_idx = q_i
                    break
            if target_r_idx is not None:
                break
    elif payload.round_index is not None and payload.question_index is not None:
        if 0 <= payload.round_index < len(rounds):
            target_round = rounds[payload.round_index]
            if 0 <= payload.question_index < len(target_round.get("questions", [])):
                target_r_idx = payload.round_index
                target_q_idx = payload.question_index
    elif payload.round_id is not None and payload.question_index is not None:
        for r_i, r in enumerate(rounds):
            if r.get("round_id") == payload.round_id or r.get("id") == payload.round_id:
                if 0 <= payload.question_index < len(r.get("questions", [])):
                    target_r_idx = r_i
                    target_q_idx = payload.question_index
                    break

    if target_r_idx is None or target_q_idx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ko'rsatilgan savol topilmadi")

    target_round = rounds[target_r_idx]
    target_q = target_round.get("questions", [])[target_q_idx]
    target_qid = target_q.get("question_id") or target_q.get("id")

    # Check if already played
    cur_svoyak_mode = (attempt.attempt_metadata or {}).get("svoyak_mode", "free_choice")
    if cur_svoyak_mode == "classic":
        next_item = get_svoyak_classic_next_question(attempt, rounds, db)
        if not next_item:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Barcha savollar o'ynab bo'lingan")
        exp_r_idx, exp_q_idx, exp_q = next_item
        if target_r_idx != exp_r_idx or target_q_idx != exp_q_idx:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Klassik rejimda savollar ketma-ket o'ynaladi. Navbatdagi savol: {rounds[exp_r_idx].get('name', 'Mavzu')} {exp_q.get('points', 10)} ball",
            )

    already_played = (
        db.query(AnswerRecord)
        .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.question_id == target_qid)
        .first()
    )
    if already_played:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bu savol allaqachon o'ynalgan")

    attempt.current_round_index = target_r_idx
    attempt.current_question_index = target_q_idx
    attempt.question_opened_at = datetime.now(timezone.utc)
    attempt.status = "in_progress"
    db.commit()

    resp = format_question_response(target_q, target_q_idx, len(target_round.get("questions", [])), target_round, attempt)
    resp["total_score"] = attempt.total_score
    resp["board"] = build_svoyak_board(attempt, rounds, db)
    resp["svoyak_mode"] = cur_svoyak_mode
    return resp


@router.post("/{session_token}/answer", status_code=status.HTTP_200_OK)
def submit_answer(
    session_token: str,
    submission: AnswerSubmission,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_session_not_expired(attempt, db)
    enforce_attempt_ownership(attempt, current_user)

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
                if is_awaiting_continue(attempt):
                    # Replay during a pending reveal: return the reveal, never the next question
                    reveal = build_record_reveal(rec, rounds) or {}
                    return {
                        "is_correct": rec.is_correct,
                        "points_awarded": rec.points_awarded,
                        "total_score": attempt.total_score,
                        "round_completed": False,
                        "quiz_completed": False,
                        "reveal_available": False,
                        "is_duplicate": True,
                        "awaiting_continue": True,
                        "correct_answer": reveal.get("correct_answer"),
                        "explanation": reveal.get("explanation"),
                        "submitted_answer": reveal.get("submitted_answer"),
                        "next_state": None,
                    }
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
                if is_awaiting_continue(attempt):
                    # Retry of the just-answered question during its reveal: never expose the next question
                    reveal = build_record_reveal(existing_record, rounds) or {}
                    return {
                        "is_correct": existing_record.is_correct,
                        "points_awarded": existing_record.points_awarded,
                        "total_score": attempt.total_score,
                        "round_completed": False,
                        "quiz_completed": False,
                        "reveal_available": False,
                        "is_duplicate": True,
                        "awaiting_continue": True,
                        "correct_answer": reveal.get("correct_answer"),
                        "explanation": reveal.get("explanation"),
                        "submitted_answer": reveal.get("submitted_answer"),
                        "next_state": None,
                    }
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

    # A new answer while a reveal is pending is rejected: the next question only opens (and its clock
    # only starts) via /continue. Accepting it would allow untimed answers with zero recorded time.
    if is_awaiting_continue(attempt):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Keyingi savolni ochish uchun «Davom etish» tugmasini bosing.",
        )

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
    # normalize_uzbek_latin folds curly/modifier apostrophes (o‘tkazish, oʻtkazish) into "'"
    is_blank = trimmed_answer == "" or normalize_uzbek_latin(trimmed_answer) in ("pass", "o'tkazish", "otkazish")

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
            # 1. Exact normalized match
            for ans in accepted_answers_data:
                norm_ans = normalize_uzbek_latin(ans.get("answer_text", ""))
                if norm_ans == cleaned_input:
                    is_correct = True
                    matched_answer_text = norm_ans
                    break

            # 2. Conservative deterministic typo tolerance (hybrid evaluation)
            if not is_correct:
                for ans in accepted_answers_data:
                    norm_ans = normalize_uzbek_latin(ans.get("answer_text", ""))
                    if is_conservative_typo_match(cleaned_input, norm_ans):
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
    is_timed_out = False
    if attempt.question_opened_at:
        q_opened = attempt.question_opened_at
        if q_opened.tzinfo is None:
            q_opened = q_opened.replace(tzinfo=timezone.utc)
        raw_delta = max(0.0, (now - q_opened).total_seconds())

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

            # Enforce server-side authoritative timeout policy:
            # In competitive/ranked modes, submissions arriving past the question limit + network grace period
            # are marked as timed out, awarded 0 points, and evaluated as incorrect.
            # Casual practice/preview sessions retain forgiving time-capping without scoring penalty.
            # Authoritative flag (same as the leaderboard): ranked attempts and legacy timed attempts are
            # strict; timed practice attempts (competitive=False) and previews get the forgiving behaviour.
            is_competitive = is_ranked_or_legacy_timed(attempt)
            grace_period = float(os.getenv("TIMER_GRACE_PERIOD_SECONDS", "3.0"))
            if is_competitive and raw_delta > (limit_val + grace_period):
                is_timed_out = True
                is_correct = False
                points = 0

            delta = min(raw_delta, limit_val)
        else:
            delta = raw_delta

        time_taken = int(round(delta))
        attempt.active_time_seconds += time_taken

    meta = {"time_taken_seconds": time_taken}
    if is_timed_out:
        meta["timed_out"] = True
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

    # Per-question reveal (correct + submitted answer always; explanation only when genuinely present)
    reveal = build_answer_reveal(current_q, raw_answer, is_blank)
    paced = is_reveal_paced(attempt, game_mode, round_type)

    # Svoya Igra Board Completion & State Check
    if game_mode == "svoyak":
        db.flush()
        all_recs = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
        answered_qids = {r.question_id for r in all_recs}
        total_board_q = sum(len(r.get("questions", [])) for r in rounds)
        is_all_done = len(answered_qids) >= total_board_q

        primary_ans = reveal["correct_answer"]

        cur_svoyak_mode = (attempt.attempt_metadata or {}).get("svoyak_mode", "free_choice")

        if is_all_done:
            attempt.question_opened_at = None
            attempt.status = "completed"
            attempt.completed_at = datetime.now(timezone.utc)
            attempt.total_correct = max(0, sum(1 for r in all_recs if r.is_correct) + demo_correct_adjustment(attempt))
            db.commit()
            board_data = build_svoyak_board(attempt, rounds, db)
            return {
                "is_correct": is_correct,
                "points_awarded": points,
                "total_score": attempt.total_score,
                "timed_out": is_timed_out,
                "correct_answer": primary_ans,
                "explanation": reveal["explanation"],
                "submitted_answer": reveal["submitted_answer"],
                "round_completed": True,
                "quiz_completed": True,
                "status": "completed",
                "board_available": True,
                "board": board_data,
                "next_state": None,
                "svoyak_mode": cur_svoyak_mode,
            }

        # Not all done
        if cur_svoyak_mode == "classic":
            next_item = get_svoyak_classic_next_question(attempt, rounds, db)
            if next_item:
                next_r_idx, next_q_idx, next_q = next_item
                attempt.current_round_index = next_r_idx
                attempt.current_question_index = next_q_idx
                attempt.status = "in_progress"
                next_state = None
                if paced:
                    # Reveal first: the next question's clock starts only when the player presses "Davom etish"
                    attempt.question_opened_at = None
                    set_awaiting_continue(attempt, True)
                    db.commit()
                else:
                    attempt.question_opened_at = datetime.now(timezone.utc)
                    db.commit()
                    next_state = format_question_response(
                        next_q, next_q_idx, len(rounds[next_r_idx].get("questions", [])), rounds[next_r_idx], attempt
                    )
                board_data = build_svoyak_board(attempt, rounds, db)
                return {
                    "is_correct": is_correct,
                    "points_awarded": points,
                    "total_score": attempt.total_score,
                    "timed_out": is_timed_out,
                    "correct_answer": primary_ans,
                    "explanation": reveal["explanation"],
                    "submitted_answer": reveal["submitted_answer"],
                    "round_completed": False,
                    "quiz_completed": False,
                    "status": "in_progress",
                    "board_available": False,
                    "board": board_data,
                    "awaiting_continue": paced,
                    "next_state": next_state,
                    "svoyak_mode": "classic",
                }

        # Free choice mode: player returns to board
        attempt.question_opened_at = None
        attempt.status = "board_selection"
        db.commit()
        board_data = build_svoyak_board(attempt, rounds, db)
        return {
            "is_correct": is_correct,
            "points_awarded": points,
            "total_score": attempt.total_score,
            "timed_out": is_timed_out,
            "correct_answer": primary_ans,
            "explanation": reveal["explanation"],
            "submitted_answer": reveal["submitted_answer"],
            "round_completed": False,
            "quiz_completed": False,
            "status": "board_selection",
            "board_available": True,
            "board": board_data,
            "next_state": None,
            "svoyak_mode": "free_choice",
        }

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
                "timed_out": is_timed_out,
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
                **(reveal if paced else {}),
            }

        attempt.status = "round_reveal"
        attempt.question_opened_at = None  # Pauses active timer during reveal
        db.commit()

        return {
            "is_correct": is_correct,
            "points_awarded": points,
            "total_score": attempt.total_score,
            "timed_out": is_timed_out,
            "round_completed": True,
            "quiz_completed": is_last_round,
            "reveal_available": True,
            "next_state": None,
            **(reveal if paced else {}),
        }

    if paced:
        # Reveal first: withhold the next question and keep its clock stopped until /continue
        attempt.question_opened_at = None
        set_awaiting_continue(attempt, True)
        db.commit()
        return {
            "is_correct": is_correct,
            "points_awarded": points,
            "total_score": attempt.total_score,
            "timed_out": is_timed_out,
            "round_completed": False,
            "quiz_completed": False,
            "reveal_available": False,
            "awaiting_continue": True,
            "next_state": None,
            **reveal,
        }

    attempt.question_opened_at = datetime.now(timezone.utc)
    db.commit()
    next_q = questions[attempt.current_question_index]
    return {
        "is_correct": is_correct,
        "points_awarded": points,
        "total_score": attempt.total_score,
        "timed_out": is_timed_out,
        "round_completed": False,
        "quiz_completed": False,
        "reveal_available": False,
        "next_state": format_question_response(next_q, attempt.current_question_index, len(questions), current_round, attempt),
    }


@router.get("/{session_token}/reveal", status_code=status.HTTP_200_OK)
def get_round_reveal(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Exposes answers and rules ONLY for the round currently completed and waiting for reveal."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_session_not_expired(attempt, db)
    enforce_attempt_ownership(attempt, current_user)

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
def continue_to_next_round(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Advances from intermediate_break or round_reveal to the next stage, or returns completed status idempotently."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_session_not_expired(attempt, db)
    enforce_attempt_ownership(attempt, current_user)

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

    # "Davom etish" after a per-question reveal: open the next question and start its clock now
    if is_awaiting_continue(attempt):
        set_awaiting_continue(attempt, False)
        attempt.question_opened_at = datetime.now(timezone.utc)
        meta = dict(attempt.attempt_metadata or {})
        meta["last_active_at"] = attempt.question_opened_at.isoformat()
        attempt.attempt_metadata = meta
        db.commit()
        next_q = questions[attempt.current_question_index]
        resp = {
            "status": "in_progress",
            "quiz_completed": False,
            "current_state": format_question_response(
                next_q, attempt.current_question_index, len(questions), current_round, attempt
            ),
        }
        if attempt.quiz_version and attempt.quiz_version.game_mode == "svoyak":
            resp["board"] = build_svoyak_board(attempt, rounds, db)
        return resp

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
        attempt.total_correct = max(0, (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.is_correct == True)
            .count()
        ) + demo_correct_adjustment(attempt))
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
def get_final_results(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Returns the end-of-quiz score breakdown once the attempt is completed."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if attempt.status == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="O'yin sessiyasi muddati tugagan (30 daqiqa faolsizlik). Yangi o'yin boshlang."
        )
    if attempt.status == "abandoned":
        # An abandoned (restarted) attempt is final: never auto-finalized into a completed/ranked result
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Bu o'yin qaytadan boshlangan. Yangi sessiyadan foydalaning."
        )
    enforce_attempt_ownership(attempt, current_user)

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
        attempt.total_correct = max(0, (
            db.query(AnswerRecord)
            .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.is_correct == True)
            .count()
        ) + demo_correct_adjustment(attempt))
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
        "is_preview": (attempt.timer_mode in ("preview", "preview_no_timer")),
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
def get_answer_review(
    session_token: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Returns full question-by-question review with explanations and answers
    ONLY for completed attempts.
    For Mantiqqasqon, the hidden_rule is safely revealed here since the quiz is completed.
    """
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    enforce_attempt_ownership(attempt, current_user)

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)

    # The full review (every round's answers) is only available once the game is over: a completed
    # attempt, or the reveal of the FINAL round. A mid-game round_reveal (multi-round packs) must not
    # expose answers of rounds that are still to be played.
    is_final_round_reveal = (
        attempt.status == "round_reveal" and attempt.current_round_index >= (len(rounds) - 1)
    )
    if attempt.status != "completed" and not is_final_round_reveal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Javoblar tahlili faqat viktorina to'liq yakunlangandan keyin ko'rsatiladi."
        )
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