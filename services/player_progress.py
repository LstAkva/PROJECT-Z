"""
Player progress: per-format statistics, per-pack progress and the resumable game.

READ-ONLY. Everything is derived from `solo_attempts` and `answer_records`; nothing is written, no
attempt is expired, abandoned or created here. (The existing profile endpoint and the play API keep
their own write paths; this module never changes game state.)

Definitions
-----------
* An attempt counts only when it is a real game: preview / staff attempts are excluded everywhere.
* Completed = status "completed".
* In progress (resumable) = an active status (in_progress, board_selection, intermediate_break,
  round_reveal) whose last activity is within the server's inactivity limit. An active attempt older
  than that limit cannot be resumed (the play API would expire it on the next request), so it is
  reported as "unfinished", not as in progress.
* Answered = distinct questions with an answer record in THAT attempt (restarts are separate attempts,
  so they never add up). Total = questions in the attempt's own published version.
* Classic Zakovat is scored by correct answers; Svoya Igra by points. The two are never added together.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import AnswerRecord, Quiz, QuizVersion, SoloAttempt

PREVIEW_TIMER_MODES = ("preview", "preview_no_timer")
ACTIVE_STATUSES = ("in_progress", "board_selection", "intermediate_break", "round_reveal")
CLASSIC_MODES = ("classic_zakovat", "zakovat_classic")
INACTIVITY_LIMIT_SECONDS = 1800  # mirrors api.play.SESSION_INACTIVITY_LIMIT_SECONDS


def _format_of(game_mode: Optional[str]) -> str:
    if game_mode in CLASSIC_MODES:
        return "classic"
    if game_mode == "svoyak":
        return "svoyak"
    return "other"


def _is_practice(meta: Optional[dict]) -> bool:
    return (meta or {}).get("competitive") is False


def _is_preview(timer_mode: Optional[str], meta: Optional[dict]) -> bool:
    return str(timer_mode or "").startswith("preview") or bool((meta or {}).get("is_staging"))


def _last_active(meta: Optional[dict], started_at: Optional[datetime]) -> Optional[datetime]:
    raw = (meta or {}).get("last_active_at")
    value = None
    if raw:
        try:
            value = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            value = None
    value = value or started_at
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def is_resumable(status: str, meta: Optional[dict], started_at: Optional[datetime], now: datetime) -> bool:
    if status not in ACTIVE_STATUSES:
        return False
    last = _last_active(meta, started_at)
    return last is not None and (now - last).total_seconds() <= INACTIVITY_LIMIT_SECONDS


def _manifest_question_count(manifest: Optional[dict]) -> Optional[int]:
    rounds = (manifest or {}).get("rounds")
    if not rounds:
        return None
    return sum(len(r.get("questions") or []) for r in rounds)


def _attempt_rows(db: Session, user_id: int):
    """One query: the player's real (non-preview) attempts with their pack and format."""
    return (
        db.query(
            SoloAttempt.id, SoloAttempt.status, SoloAttempt.timer_mode, SoloAttempt.total_score,
            SoloAttempt.total_correct, SoloAttempt.attempt_metadata, SoloAttempt.started_at,
            SoloAttempt.completed_at, SoloAttempt.quiz_version_id, SoloAttempt.session_token,
            QuizVersion.quiz_id, QuizVersion.game_mode,
        )
        .join(QuizVersion, SoloAttempt.quiz_version_id == QuizVersion.id)
        .filter(SoloAttempt.user_id == user_id, SoloAttempt.timer_mode.notin_(PREVIEW_TIMER_MODES))
        .order_by(SoloAttempt.id.desc())
        .all()
    )


def _answered_counts(db: Session, attempt_ids: Iterable[int]) -> Dict[int, int]:
    ids = list(attempt_ids)
    if not ids:
        return {}
    rows = (
        db.query(AnswerRecord.attempt_id, func.count(func.distinct(AnswerRecord.question_id)))
        .filter(AnswerRecord.attempt_id.in_(ids))
        .group_by(AnswerRecord.attempt_id)
        .all()
    )
    return {attempt_id: count for attempt_id, count in rows}


def _version_totals(db: Session, version_ids: Iterable[int]) -> Dict[int, Optional[int]]:
    ids = list(set(version_ids))
    if not ids:
        return {}
    rows = db.query(QuizVersion.id, QuizVersion.published_manifest).filter(QuizVersion.id.in_(ids)).all()
    return {vid: _manifest_question_count(manifest) for vid, manifest in rows}


# --------------------------------------------------------------------------- per-format statistics

def format_statistics(db: Session, user_id: int) -> Dict[str, Any]:
    """Competitive statistics per format (practice and preview runs excluded, practice counted apart).

    classic: games, correct, answered (answer records), accuracy = correct / answered.
    svoyak:  games, total_points, best_points, correct, answered.
    """
    rows = [r for r in _attempt_rows(db, user_id) if r.status == "completed"
            and not _is_preview(r.timer_mode, r.attempt_metadata)]
    competitive = [r for r in rows if not _is_practice(r.attempt_metadata)]
    answered = {}
    if competitive:
        answered = dict(
            db.query(AnswerRecord.attempt_id, func.count(AnswerRecord.id))
            .filter(AnswerRecord.attempt_id.in_([r.id for r in competitive]))
            .group_by(AnswerRecord.attempt_id)
            .all()
        )

    def block(fmt):
        games = [r for r in competitive if _format_of(r.game_mode) == fmt]
        correct = sum(r.total_correct or 0 for r in games)
        total_answered = sum(answered.get(r.id, 0) for r in games)
        data = {
            "games": len(games),
            "correct": correct,
            "answered": total_answered,
            "accuracy_percent": round(correct / total_answered * 100, 1) if total_answered else None,
        }
        if fmt == "svoyak":
            data["total_points"] = sum(r.total_score or 0 for r in games)
            data["best_points"] = max((r.total_score or 0 for r in games), default=None)
        return data

    return {
        "classic": block("classic"),
        "svoyak": block("svoyak"),
        "other_games": sum(1 for r in competitive if _format_of(r.game_mode) == "other"),
        "practice_games": len(rows) - len(competitive),
    }


# --------------------------------------------------------------------------- per-pack progress

def pack_progress(db: Session, user_id: int, now: Optional[datetime] = None) -> Dict[int, Dict[str, Any]]:
    """For every pack the player has touched: in_progress (with answered/total), completed (with the best
    result in that pack's own scoring) or unfinished. Packs never started are absent."""
    now = now or datetime.now(timezone.utc)
    rows = [r for r in _attempt_rows(db, user_id) if not _is_preview(r.timer_mode, r.attempt_metadata)]
    by_quiz: Dict[int, List[Any]] = {}
    for r in rows:
        by_quiz.setdefault(r.quiz_id, []).append(r)

    live = {q: next((r for r in rs if is_resumable(r.status, r.attempt_metadata, r.started_at, now)), None)
            for q, rs in by_quiz.items()}
    live_rows = [r for r in live.values() if r is not None]
    answered = _answered_counts(db, [r.id for r in live_rows])
    totals = _version_totals(db, [r.quiz_version_id for r in live_rows])

    result: Dict[int, Dict[str, Any]] = {}
    for quiz_id, attempts in by_quiz.items():
        fmt = _format_of(attempts[0].game_mode)
        completed = [r for r in attempts if r.status == "completed"]
        best = None
        if completed:
            if fmt == "svoyak":
                best = {"points": max(r.total_score or 0 for r in completed)}
            else:
                best = {"correct": max(r.total_correct or 0 for r in completed)}
        current = live[quiz_id]
        if current is not None:
            result[quiz_id] = {
                "state": "in_progress",
                "format": fmt,
                "answered": answered.get(current.id, 0),
                "total": totals.get(current.quiz_version_id),
                "completed_before": bool(completed),
                "best": best,
            }
        elif completed:
            result[quiz_id] = {"state": "completed", "format": fmt, "best": best,
                               "completed_count": len(completed)}
        else:
            result[quiz_id] = {"state": "unfinished", "format": fmt}
    return result


# --------------------------------------------------------------------------- the game to resume

def resumable_game(db: Session, user_id: int, quiz_id: Optional[int] = None,
                   now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The player's most recent resumable game (optionally for one pack), or None.

    Only attempts the play API would still accept are returned; the client restores them through the
    existing GET /api/play/{session_token}, so no state is rebuilt in the browser."""
    now = now or datetime.now(timezone.utc)
    for r in _attempt_rows(db, user_id):
        if quiz_id is not None and r.quiz_id != quiz_id:
            continue
        if _is_preview(r.timer_mode, r.attempt_metadata):
            continue
        if not is_resumable(r.status, r.attempt_metadata, r.started_at, now):
            continue
        quiz = db.query(Quiz.title).filter(Quiz.id == r.quiz_id).first()
        manifest = db.query(QuizVersion.published_manifest).filter(QuizVersion.id == r.quiz_version_id).scalar()
        title = (manifest or {}).get("title") or (quiz.title if quiz else None)
        answered = _answered_counts(db, [r.id]).get(r.id, 0)
        fmt = _format_of(r.game_mode)
        return {
            "session_token": r.session_token,
            "quiz_id": r.quiz_id,
            "title": title,
            "format": fmt,
            "game_mode": r.game_mode,
            "status": r.status,
            "answered": answered,
            "total": _manifest_question_count(manifest),
            "score": r.total_score if fmt == "svoyak" else None,
            "correct": r.total_correct if fmt != "svoyak" else None,
            "ranked": not _is_practice(r.attempt_metadata),
            "last_active_at": _last_active(r.attempt_metadata, r.started_at).isoformat(),
        }
    return None
