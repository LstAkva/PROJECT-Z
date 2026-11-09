import os
import json
import threading
from zoneinfo import ZoneInfo
from datetime import datetime, date, timezone
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from models import DailyQuestion, DailySubmission, User
from services.gameplay import normalize_uzbek_latin, is_conservative_typo_match

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")
_SCHEDULING_LOCK = threading.Lock()
_POOL_LOCK = threading.Lock()


def get_tashkent_now() -> datetime:
    """Returns the current timezone-aware datetime in Asia/Tashkent."""
    return datetime.now(TASHKENT_TZ)


def get_tashkent_today() -> date:
    """Returns the current calendar date in Asia/Tashkent (UTC+5)."""
    return datetime.now(TASHKENT_TZ).date()


_VERIFIED_POOL_CACHE: Optional[List[Dict[str, Any]]] = None


def get_verified_pool() -> List[Dict[str, Any]]:
    """Loads and caches the audited and verified Daily Question candidates."""
    global _VERIFIED_POOL_CACHE
    if _VERIFIED_POOL_CACHE is None:
        with _POOL_LOCK:
            if _VERIFIED_POOL_CACHE is None:
                # Primary source: dedicated tracked production Daily Question bank
                primary_file = os.path.normpath(
                    os.path.join(os.path.dirname(__file__), "..", "data", "daily", "daily_questions_bank.json")
                )
                fallback_file = os.path.normpath(
                    os.path.join(os.path.dirname(__file__), "..", "data", "eski_savollar_extracted", "verified_daily_pool_33.json")
                )
                target_file = primary_file if os.path.exists(primary_file) else fallback_file
                if os.path.exists(target_file):
                    with open(target_file, "r", encoding="utf-8") as f:
                        _VERIFIED_POOL_CACHE = json.load(f)
                else:
                    _VERIFIED_POOL_CACHE = []
    return _VERIFIED_POOL_CACHE


def is_candidate_eligible(candidate: Dict[str, Any]) -> bool:
    """
    Strict Conservative Standalone Filter for Daily Question:
    1. Standalone non-empty question text.
    2. Exactly one non-empty primary answer.
    3. No essential missing media dependencies.
    4. No multi-part / emoji triplet bundling (1⃣, 2⃣, 3⃣).
    5. Valid source metadata.
    """
    if not candidate or not isinstance(candidate, dict):
        return False

    text = candidate.get("clean_text") or candidate.get("text") or ""
    ans = candidate.get("primary_answer") or ""
    if not text.strip() or not ans.strip():
        return False

    # Skip multi-part questions or emoji triplet enumerations
    if any(marker in text for marker in ["1⃣", "2⃣", "3⃣", "4⃣", "5⃣"]):
        return False

    # Skip missing media dependencies
    source_meta = candidate.get("source_meta") or {}
    has_media_url = bool(source_meta.get("media_url"))
    if not has_media_url:
        from services.eski_savollar_extractor import RE_DIRECT_VISUAL_INSTRUCTION
        if RE_DIRECT_VISUAL_INSTRUCTION.search(text):
            return False
        if source_meta.get("has_media"):
            text_lower = text.lower()
            if any(w in text_lower for w in ["suratdagi", "rasmdagi", "ushbu suratda", "rasmda"]):
                return False

    # Check for corrupt / unplayable indicators
    if candidate.get("daily_question_status") == "blocked":
        return False

    return True


def get_or_schedule_daily_question(
    db: Session,
    target_date: Optional[date] = None,
    pool: Optional[List[Dict[str, Any]]] = None
) -> Optional[DailyQuestion]:
    """
    Retrieves the persistent Daily Question for target_date (defaulting to Asia/Tashkent today).
    If none exists yet, schedules the next available chronological eligible candidate from the verified pool.
    Prevents repeats until pool is exhausted.
    If pool is exhausted, returns None (explicit unavailable state).
    Concurrent requests cannot schedule duplicate questions for the same date.
    """
    if target_date is None:
        target_date = get_tashkent_today()

    with _SCHEDULING_LOCK:
        # Check existing scheduled question under lock
        existing = db.query(DailyQuestion).filter(DailyQuestion.schedule_date == target_date).first()
        if existing:
            return existing

        # 2. Schedule next eligible candidate
        candidate_pool = pool if pool is not None else get_verified_pool()
        if not candidate_pool:
            return None

        # Get set of all message_ids already scheduled in DB
        scheduled_mids = set(r[0] for r in db.query(DailyQuestion.message_id).all())

        candidate_to_schedule = None
        for cand in candidate_pool:
            mid = cand.get("message_id")
            if mid not in scheduled_mids and is_candidate_eligible(cand):
                candidate_to_schedule = cand
                break

        if not candidate_to_schedule:
            # Pool exhausted!
            return None

        # Create persistent DailyQuestion
        new_dq = DailyQuestion(
            schedule_date=target_date,
            question_id=candidate_to_schedule["question_id"],
            message_id=candidate_to_schedule["message_id"],
            channel_id=candidate_to_schedule.get("channel_id", 2010763825),
            question_text=candidate_to_schedule["clean_text"],
            primary_answer=candidate_to_schedule["primary_answer"],
            explanation=candidate_to_schedule.get("explanation"),
            accepted_answers=candidate_to_schedule.get("accepted_answers", [candidate_to_schedule["primary_answer"]]),
            source_meta=candidate_to_schedule.get("source_meta", {}),
            created_at=datetime.now(timezone.utc)
        )

        try:
            db.add(new_dq)
            db.commit()
            return db.query(DailyQuestion).filter(DailyQuestion.schedule_date == target_date).first()
        except Exception:
            db.rollback()
            return db.query(DailyQuestion).filter(DailyQuestion.schedule_date == target_date).first()


def evaluate_daily_answer(submitted_text: str, daily_question: DailyQuestion) -> Tuple[bool, str]:
    """
    Evaluates a player's answer against the Daily Question.
    Rules:
    - Uses normalize_uzbek_latin for case, apostrophes, and punctuation.
    - Compares against primary_answer and separately stored accepted_answers.
    - Tolerates minor typos using is_conservative_typo_match.
    - Keeps evaluation logic completely separate from the immutable source text.
    """
    if not submitted_text or not submitted_text.strip():
        return False, "empty_submission"

    sub_norm = normalize_uzbek_latin(submitted_text)
    target_norm = normalize_uzbek_latin(daily_question.primary_answer)

    accepted_norms = {target_norm}
    for ans in (daily_question.accepted_answers or []):
        norm_ans = normalize_uzbek_latin(ans)
        if norm_ans:
            accepted_norms.add(norm_ans)

    # 1. Exact normalized match
    if sub_norm in accepted_norms:
        return True, "exact_match"

    # 2. Conservative typo match
    for target in accepted_norms:
        if is_conservative_typo_match(sub_norm, target):
            return True, "typo_match"

    return False, "no_match"


MAX_DAILY_ATTEMPTS = 3


def submit_daily_answer(
    db: Session,
    daily_question: DailyQuestion,
    user: User,
    submitted_text: str,
    client_ip: str = "unknown"
) -> Tuple[DailySubmission, bool]:
    """
    Records or updates a user's submission for today's Daily Question.
    Allows up to MAX_DAILY_ATTEMPTS (3) attempts per question.
    Returns (DailySubmission, already_finished: bool).
    """
    clean_text = submitted_text.strip()
    if not clean_text:
        raise ValueError("Javob bo'sh bo'lishi mumkin emas.")

    # Check if already submitted
    existing = (
        db.query(DailySubmission)
        .filter(
            DailySubmission.daily_question_id == daily_question.id,
            DailySubmission.user_id == user.id
        )
        .first()
    )

    if existing:
        meta = existing.submission_meta or {}
        attempts = meta.get("attempts", 1)
        history = list(meta.get("history", [existing.submitted_text]))
        is_finished = existing.is_correct or meta.get("is_finished", False) or (attempts >= MAX_DAILY_ATTEMPTS)

        if is_finished:
            return existing, True

        # Process next attempt
        is_correct, match_type = evaluate_daily_answer(clean_text, daily_question)
        attempts += 1
        if clean_text not in history:
            history.append(clean_text)
        is_finished = is_correct or (attempts >= MAX_DAILY_ATTEMPTS)

        existing.submitted_text = clean_text
        if is_correct:
            existing.is_correct = True
        existing.answered_at = datetime.now(timezone.utc)
        existing.submission_meta = {
            "attempts": attempts,
            "history": history,
            "is_finished": is_finished,
            "match_type": match_type,
            "client_ip": client_ip
        }
        db.commit()
        db.refresh(existing)
        return existing, False

    # First attempt
    is_correct, match_type = evaluate_daily_answer(clean_text, daily_question)
    is_finished = is_correct or (1 >= MAX_DAILY_ATTEMPTS)

    sub = DailySubmission(
        daily_question_id=daily_question.id,
        user_id=user.id,
        submitted_text=clean_text,
        is_correct=is_correct,
        answered_at=datetime.now(timezone.utc),
        submission_meta={
            "attempts": 1,
            "history": [clean_text],
            "is_finished": is_finished,
            "match_type": match_type,
            "client_ip": client_ip
        }
    )

    try:
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub, False
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(DailySubmission)
            .filter(
                DailySubmission.daily_question_id == daily_question.id,
                DailySubmission.user_id == user.id
            )
            .first()
        )
        if existing:
            return existing, True
        raise
