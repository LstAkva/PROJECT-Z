"""
Daily Question engagement: streaks, the shareable result text and the question-bank status.

Everything here is READ-ONLY. It derives its answers from records the Daily feature already writes
(`daily_questions`, `daily_submissions`) and from the same pool and eligibility rule the scheduler uses.
It never schedules a question, never writes a submission and never touches the bank file.

Streak rules
------------
* A participation day is the Asia/Tashkent `schedule_date` of a Daily Question the player has a
  submission for. A submission row exists only after a non-empty, evaluated attempt, and `/submit`
  only ever accepts TODAY's question by the server's clock, so a device clock or timezone cannot
  create, move or back-date a day. The unique (question, user) constraint makes several attempts on
  one day a single participation day.
* Participation counts, not correctness: a day on which every attempt was wrong still counts.
* Consecutive means consecutive calendar days. A calendar day on which NO question was scheduled
  (the bank was empty, or nobody opened the page that day) is neutral: it neither breaks nor extends a
  streak, because nobody could have played it.
* The current streak is still "alive" while today's question has not been answered yet; it is shown as
  at risk until the player answers. It is broken once a scheduled day passes unanswered.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from models import DailyQuestion, DailySubmission

# --------------------------------------------------------------------------- streaks


def _streak_from_dates(participation: List[date], scheduled: Set[date], today: date) -> Dict[str, Any]:
    """Pure streak arithmetic (no database), so the rules can be tested exhaustively."""
    days = sorted(set(d for d in participation if d <= today))
    if not days:
        return {"current": 0, "best": 0, "total_days": 0, "last_date": None, "today_done": False, "at_risk": False}

    def bridged(a: date, b: date) -> bool:
        """True when every calendar day strictly between a and b had no scheduled question."""
        d = a + timedelta(days=1)
        while d < b:
            if d in scheduled:
                return False
            d += timedelta(days=1)
        return True

    best = run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if bridged(prev, cur) else 1
        best = max(best, run)

    last = days[-1]
    today_done = last == today
    # The latest run is current if today was answered, or if no scheduled day was missed since `last`.
    current = run if (today_done or bridged(last, today)) else 0
    return {
        "current": current,
        "best": best,
        "total_days": len(days),
        "last_date": last.isoformat(),
        "today_done": today_done,
        "at_risk": current > 0 and not today_done,
    }


def compute_daily_streak(db: Session, user_id: int, today: date) -> Dict[str, Any]:
    """Current / best streak, participation days and last date for one player. Two narrow queries."""
    participation = [
        row[0]
        for row in db.query(DailyQuestion.schedule_date)
        .join(DailySubmission, DailySubmission.daily_question_id == DailyQuestion.id)
        .filter(DailySubmission.user_id == user_id)
        .all()
    ]
    if not participation:
        return _streak_from_dates([], set(), today)
    first = min(participation)
    scheduled = {
        row[0]
        for row in db.query(DailyQuestion.schedule_date)
        .filter(DailyQuestion.schedule_date >= first, DailyQuestion.schedule_date <= today)
        .all()
    }
    return _streak_from_dates(participation, scheduled, today)


# --------------------------------------------------------------------------- share text

DEFAULT_PUBLIC_SITE_URL = "https://zakowhat.uz"


def public_site_url() -> str:
    """The link printed in shared results. PUBLIC_SITE_URL is optional; an unset, empty or malformed
    value (anything that is not a plain http(s) origin) falls back to the public domain."""
    raw = (os.getenv("PUBLIC_SITE_URL") or "").strip().rstrip("/")
    if raw.startswith(("https://", "http://")) and " " not in raw and len(raw) > len("https://"):
        return raw
    return DEFAULT_PUBLIC_SITE_URL


def build_share_text(*, schedule_date: date, is_correct: bool, attempts_used: Optional[int],
                     max_attempts: int, current_streak: int) -> str:
    """Short text a player can paste into Telegram. It never contains the question, the answer,
    the player's name or any id: only the date, how it went and the streak."""
    lines = []
    if is_correct:
        lines.append("🔥 Bugun ham Kun savolini yechdim!" if current_streak >= 2 else "🧠 Bugun Kun savolini yechdim!")
    else:
        lines.append("🧠 Bugungi Kun savoli oson emas edi.")
    lines.append(f"📅 {schedule_date.strftime('%d.%m.%Y')}")
    if is_correct and attempts_used:
        lines.append(f"🎯 {attempts_used}-urinishda topdim" if attempts_used > 1 else "🎯 Birinchi urinishda topdim")
    elif not is_correct and attempts_used:
        lines.append(f"🎯 {attempts_used} ta urinishda topa olmadim")
    if current_streak >= 2:
        lines.append(f"🔥 {current_streak} kunlik streak!")
    lines.append("")
    lines.append("ZakoWhat — Intelligence Meets Culture.")
    lines.append(f"{public_site_url()}/daily")
    return "\n".join(lines)


# --------------------------------------------------------------------------- bank monitoring

# Fields the scheduler reads unconditionally when it creates the day's question
# (services.daily_question.get_or_schedule_daily_question): an entry without them cannot be scheduled.
REQUIRED_FIELDS = ("question_id", "message_id", "clean_text", "primary_answer")

WARNING_AT = 30
CRITICAL_AT = 10

_ANALYSIS_CACHE: Dict[str, Any] = {"pool_id": None, "result": None}
_ANALYSIS_LOCK = Lock()


def _analyse_pool(pool: List[Any], eligible_fn) -> Dict[str, Any]:
    """Static facts about the bank file (cached per loaded pool object; the file does not change at runtime)."""
    with _ANALYSIS_LOCK:
        if _ANALYSIS_CACHE["pool_id"] == id(pool) and _ANALYSIS_CACHE["result"] is not None:
            return _ANALYSIS_CACHE["result"]
        invalid = 0
        ineligible = 0
        eligible_ids: List[Any] = []
        for entry in pool:
            if not isinstance(entry, dict) or any(not entry.get(f) and entry.get(f) != 0 for f in REQUIRED_FIELDS):
                invalid += 1
                continue
            if not eligible_fn(entry):
                ineligible += 1
                continue
            eligible_ids.append(entry["message_id"])
        distinct = set(eligible_ids)
        result = {
            "total_entries": len(pool),
            "invalid_entries": invalid,
            "ineligible_entries": ineligible,
            "eligible_entries": len(eligible_ids),
            "duplicate_entries": len(eligible_ids) - len(distinct),
            "eligible_ids": distinct,
            "all_ids": {e.get("message_id") for e in pool if isinstance(e, dict)},
        }
        _ANALYSIS_CACHE["pool_id"] = id(pool)
        _ANALYSIS_CACHE["result"] = result
        return result


def bank_level(remaining: int) -> str:
    if remaining <= 0:
        return "empty"
    if remaining <= CRITICAL_AT:
        return "critical"
    if remaining <= WARNING_AT:
        return "warning"
    return "normal"


BANK_MESSAGES = {
    "normal": "Kun savoli banki: {n} ta savol qoldi.",
    "warning": "Diqqat! Kun savoli bankida savollar kam qoldi: {n} ta.",
    "critical": "Juda kam savol qoldi ({n} ta)! Yangi savollar qo‘shish kerak.",
    "empty": "Kun savoli bankida foydalanish mumkin bo‘lgan savollar qolmagan.",
}


def compute_bank_status(db: Session, pool: List[Any], today: date, eligible_fn) -> Dict[str, Any]:
    """How many more days the bank can serve, using the scheduler's own pool and eligibility rule.

    A question can still be scheduled when it is valid, eligible and its message_id has never been
    scheduled (the scheduler's uniqueness key). Duplicate message_ids in the file count once."""
    facts = _analyse_pool(pool, eligible_fn)
    scheduled_rows = db.query(DailyQuestion.message_id, DailyQuestion.schedule_date).all()
    scheduled_ids = {mid for mid, _ in scheduled_rows}
    scheduled_dates = {d for _, d in scheduled_rows}

    remaining_ids = facts["eligible_ids"] - scheduled_ids
    remaining = len(remaining_ids)
    used_from_bank = len(facts["eligible_ids"] & scheduled_ids)
    level = bank_level(remaining)

    # Today uses a question only when it has not been scheduled yet.
    future_scheduled = sum(1 for d in scheduled_dates if d > today)
    first_day_needing_bank = today if today not in scheduled_dates else today + timedelta(days=1)
    first_day_needing_bank += timedelta(days=future_scheduled)
    last_available = first_day_needing_bank + timedelta(days=remaining - 1) if remaining > 0 else None

    return {
        "total_entries": facts["total_entries"],
        "invalid_entries": facts["invalid_entries"],
        "ineligible_entries": facts["ineligible_entries"],
        "eligible_entries": facts["eligible_entries"],
        "duplicate_entries": facts["duplicate_entries"],
        "eligible_unique": len(facts["eligible_ids"]),
        "used": used_from_bank,
        "scheduled_days_total": len(scheduled_rows),
        "scheduled_outside_bank": len(scheduled_ids - facts["all_ids"]),
        "remaining": remaining,
        "level": level,
        "message": BANK_MESSAGES[level].format(n=remaining),
        "today_scheduled": today in scheduled_dates,
        "last_available_date": last_available.isoformat() if last_available else None,
        "thresholds": {"warning": WARNING_AT, "critical": CRITICAL_AT},
    }
