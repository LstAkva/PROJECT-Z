"""
Owner Control Mode — demonstration sessions (owner only).

A demo session is an ordinary SoloAttempt with
    timer_mode = "preview" | "preview_no_timer"   (the existing staff/preview class) and
    attempt_metadata["is_demo"] = True, ["competitive"] = False.
The preview class is already excluded everywhere a result could count: leaderboards
(api.quizzes.fetch_version_leaderboard), ranked status (api.play.is_ranked_or_legacy_timed), profile
statistics, Arena progress and homepage resume (services.player_progress), and it never touches the Daily
tables. No schema change is needed.

Every endpoint here:
  * requires verify_owner_access (401 anonymous, 403 for every non-owner, fail-closed without config);
  * only acts on a session that is a demo AND belongs to the calling owner — an owner can never reach
    another player's game or any ranked/practice game, their own included;
  * changes state on the server; the client only re-reads it. Nothing here goes through the normal
    answer submission, so no control can create an answer by accident.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.auth import verify_owner_access
from api.play import (
    build_svoyak_board,
    calculate_question_time_limit,
    clean_explanation,
    enforce_session_not_expired,
    format_question_response,
    get_manifest_or_db_rounds,
    resolve_primary_answer,
)
from database import get_db
from models import AnswerRecord, Quiz, QuizVersion, SoloAttempt, User

router = APIRouter(prefix="/api/demo", tags=["owner-demo"])

DEMO_TIMER_MODES = ("preview", "preview_no_timer")
MAX_SCORE_STEP = 1000           # one manual adjustment, either direction
MAX_TIME_STEP = 600             # seconds added/removed in one step


class DemoStartRequest(BaseModel):
    timer_mode: Optional[str] = "with_timer"


class JumpRequest(BaseModel):
    round_index: int = Field(..., ge=0)
    question_index: int = Field(..., ge=0)


class TimerRequest(BaseModel):
    action: str                      # pause | resume | add | reset | expire
    seconds: int = 0


class ScoreRequest(BaseModel):
    action: str                      # add | reset
    delta: int = 0


# ----------------------------------------------------------------------------- helpers

def is_demo_attempt(attempt: Optional[SoloAttempt]) -> bool:
    return bool(attempt) and attempt.timer_mode in DEMO_TIMER_MODES and bool((attempt.attempt_metadata or {}).get("is_demo"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def load_owned_demo(session_token: str, owner: User, db: Session) -> SoloAttempt:
    """The only way into a session from this router: it must exist, be a demo and belong to this owner.
    Every other case answers 404, so the endpoint reveals nothing about other people's sessions."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt or attempt.user_id != owner.id or not is_demo_attempt(attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Namoyish sessiyasi topilmadi")
    enforce_session_not_expired(attempt, db)
    return attempt


def _meta(attempt: SoloAttempt) -> Dict[str, Any]:
    return dict(attempt.attempt_metadata or {})


def _save_meta(attempt: SoloAttempt, meta: Dict[str, Any]) -> None:
    meta["last_active_at"] = _now().isoformat()
    attempt.attempt_metadata = meta


def _game_mode(attempt: SoloAttempt) -> str:
    return (attempt.quiz_version.game_mode if attempt.quiz_version else None) or "classic_zakovat"


def _rounds(attempt: SoloAttempt, db: Session) -> List[Dict[str, Any]]:
    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    if not rounds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="To‘plamda savollar yo‘q")
    return rounds


def _question_at(rounds: List[Dict[str, Any]], r_idx: int, q_idx: int) -> Dict[str, Any]:
    if r_idx >= len(rounds) or q_idx >= len(rounds[r_idx].get("questions", [])):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bunday savol yo‘q")
    return rounds[r_idx]["questions"][q_idx]


def _flat_positions(rounds: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
    return [(r, q) for r, rd in enumerate(rounds) for q in range(len(rd.get("questions", [])))]


def _question_limit(attempt: SoloAttempt, rounds: List[Dict[str, Any]]) -> int:
    """The same limit the gameplay endpoints use (0 when the demo runs without a timer)."""
    if attempt.timer_mode == "preview_no_timer" or (attempt.attempt_metadata or {}).get("timer_enabled") is False:
        return 0
    r_idx, q_idx = attempt.current_round_index, attempt.current_question_index
    if r_idx >= len(rounds) or q_idx >= len(rounds[r_idx].get("questions", [])):
        return 0
    rnd = rounds[r_idx]
    cfg_limit = (rnd.get("config") or {}).get("time_limit_seconds")
    if cfg_limit and int(cfg_limit) > 0:
        return int(cfg_limit)
    return calculate_question_time_limit(rnd["questions"][q_idx].get("text", ""), rnd.get("round_type", "zakovat_classic"))


def _question_is_open(attempt: SoloAttempt) -> bool:
    return attempt.status == "in_progress" and attempt.question_opened_at is not None \
        and not (attempt.attempt_metadata or {}).get("awaiting_continue")


def recompute_totals(attempt: SoloAttempt, db: Session) -> None:
    """Totals = what the recorded answers earned + the owner's manual adjustment (kept apart, so it is
    always visible and can be reset)."""
    records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    adjustment = int(_meta(attempt).get("demo_score_adjustment", 0))
    correct = sum(1 for r in records if r.is_correct)
    points = sum(int(r.points_awarded or 0) for r in records)
    attempt.total_score = points + adjustment
    # Classic Zakovat counts correct answers, so a manual point there is a manual correct answer.
    attempt.total_correct = max(0, correct + (adjustment if _game_mode(attempt) != "svoyak" else 0))


def open_question(attempt: SoloAttempt, rounds: List[Dict[str, Any]], r_idx: int, q_idx: int, db: Session) -> None:
    """Puts one question on screen with a fresh clock. Any answer already given to it is removed first, so
    the question is genuinely replayable and the totals stay consistent."""
    question = _question_at(rounds, r_idx, q_idx)
    qid = question.get("question_id") or question.get("id")
    db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.question_id == qid) \
        .delete(synchronize_session=False)
    attempt.current_round_index = r_idx
    attempt.current_question_index = q_idx
    attempt.status = "in_progress"
    attempt.completed_at = None
    attempt.question_opened_at = _now()
    meta = _meta(attempt)
    meta.pop("demo_paused_at", None)
    meta.pop("awaiting_continue", None)
    _save_meta(attempt, meta)
    recompute_totals(attempt, db)


def back_to_board(attempt: SoloAttempt) -> None:
    attempt.status = "board_selection"
    attempt.question_opened_at = None
    meta = _meta(attempt)
    meta.pop("demo_paused_at", None)
    meta.pop("awaiting_continue", None)
    _save_meta(attempt, meta)


def finish(attempt: SoloAttempt, db: Session) -> None:
    attempt.status = "completed"
    attempt.completed_at = _now()
    attempt.question_opened_at = None
    meta = _meta(attempt)
    meta.pop("demo_paused_at", None)
    meta.pop("awaiting_continue", None)
    _save_meta(attempt, meta)
    recompute_totals(attempt, db)


def demo_status(attempt: SoloAttempt, db: Session) -> Dict[str, Any]:
    """Everything the Owner Controls panel shows. Only ever returned to the owner of the demo."""
    rounds = _rounds(attempt, db)
    meta = attempt.attempt_metadata or {}
    answered = {r.question_id for r in db.query(AnswerRecord.question_id).filter(AnswerRecord.attempt_id == attempt.id)}
    outline = []
    for r_idx, rnd in enumerate(rounds):
        title = (rnd.get("config") or {}).get("theme_title") or rnd.get("name") or f"{r_idx + 1}-tur"
        outline.append({"round_index": r_idx, "title": title, "questions": [
            {"question_index": q_idx, "label": str(q.get("points")) if _game_mode(attempt) == "svoyak" else str(q_idx + 1),
             "answered": (q.get("question_id") or q.get("id")) in answered}
            for q_idx, q in enumerate(rnd.get("questions", []))]})
    limit = _question_limit(attempt, rounds)
    remaining = None
    if limit and attempt.question_opened_at is not None:
        end = _aware(datetime.fromisoformat(meta["demo_paused_at"])) if meta.get("demo_paused_at") else _now()
        # same rounding as format_question_response, so the panel and the game clock always agree
        remaining = max(0, limit - int((end - _aware(attempt.question_opened_at)).total_seconds()))
    return {
        "demo": True,
        "session_token": attempt.session_token,
        "game_mode": _game_mode(attempt),
        "status": attempt.status,
        "position": {"round_index": attempt.current_round_index, "question_index": attempt.current_question_index},
        "question_open": _question_is_open(attempt),
        "awaiting_continue": bool(meta.get("awaiting_continue")),
        "timer": {"enabled": bool(limit), "limit_seconds": limit, "remaining_seconds": remaining,
                  "paused": bool(meta.get("demo_paused_at"))},
        "score": {"total_score": attempt.total_score, "total_correct": attempt.total_correct,
                  "manual_adjustment": int(meta.get("demo_score_adjustment", 0)),
                  "modified": bool(meta.get("demo_score_modified"))},
        "outline": outline,
    }


# ----------------------------------------------------------------------------- endpoints

@router.post("/start/{quiz_id}", status_code=status.HTTP_201_CREATED)
def start_demo(quiz_id: int, payload: Optional[DemoStartRequest] = None, db: Session = Depends(get_db),
               owner: User = Depends(verify_owner_access)):
    """Starts a demonstration of a published pack. Always a demo: it can never become a ranked result."""
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    version = None
    if quiz:
        version = (db.query(QuizVersion)
                   .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
                   .order_by(QuizVersion.version_number.desc()).first())
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nashr qilingan to‘plam topilmadi")
    rounds = _rounds_for(version, db)

    with_timer = (payload.timer_mode if payload and payload.timer_mode else "with_timer") != "no_timer"
    now = _now()
    is_svoyak = version.game_mode == "svoyak"
    attempt = SoloAttempt(
        quiz_version_id=version.id,
        user_id=owner.id,
        anon_id=None,
        timer_mode="preview" if with_timer else "preview_no_timer",
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        total_correct=0,
        active_time_seconds=0,
        # Svoya Igra demos open on the board (free choice: the owner can pick any cell in any order).
        question_opened_at=None if is_svoyak else now,
        status="board_selection" if is_svoyak else "in_progress",
        attempt_metadata={
            "is_demo": True,
            "competitive": False,
            "timer_mode": "with_timer" if with_timer else "no_timer",
            "timer_enabled": with_timer,
            "svoyak_mode": "free_choice",
            "last_active_at": now.isoformat(),
        },
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    response = {
        "session_token": attempt.session_token,
        "quiz_title": quiz.title,
        "game_mode": version.game_mode,
        "timer_mode": "with_timer" if with_timer else "no_timer",
        "ranked": False,
        "demo": True,
        "total_score": 0,
    }
    if is_svoyak:
        response.update({"status": "board_selection", "svoyak_mode": "free_choice",
                         "board": build_svoyak_board(attempt, rounds, db), "current_state": None})
    else:
        first = rounds[0]
        response["current_state"] = format_question_response(first["questions"][0], 0, len(first["questions"]), first, attempt)
    return response


def _rounds_for(version: QuizVersion, db: Session) -> List[Dict[str, Any]]:
    rounds = get_manifest_or_db_rounds(version, db)
    if not rounds or not rounds[0].get("questions"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="To‘plamda savollar yo‘q")
    return rounds


@router.get("/{session_token}")
def get_demo_status(session_token: str, db: Session = Depends(get_db), owner: User = Depends(verify_owner_access)):
    return demo_status(load_owned_demo(session_token, owner, db), db)


@router.post("/{session_token}/jump")
def jump_to_question(session_token: str, payload: JumpRequest, db: Session = Depends(get_db),
                     owner: User = Depends(verify_owner_access)):
    """Opens any question of the pack (also: restart a question, go back to an earlier one)."""
    attempt = load_owned_demo(session_token, owner, db)
    rounds = _rounds(attempt, db)
    open_question(attempt, rounds, payload.round_index, payload.question_index, db)
    db.commit()
    return demo_status(attempt, db)


@router.post("/{session_token}/skip")
def skip_question(session_token: str, db: Session = Depends(get_db), owner: User = Depends(verify_owner_access)):
    """Classic Zakovat: the next question in order (past the last one the demo ends).
    Svoya Igra: leaves the current cell unplayed and returns to the board."""
    attempt = load_owned_demo(session_token, owner, db)
    rounds = _rounds(attempt, db)
    if _game_mode(attempt) == "svoyak":
        back_to_board(attempt)
    else:
        order = _flat_positions(rounds)
        here = (attempt.current_round_index, attempt.current_question_index)
        nxt = order.index(here) + 1 if here in order else 0
        if nxt >= len(order):
            finish(attempt, db)
        else:
            attempt_r, attempt_q = order[nxt]
            open_question(attempt, rounds, attempt_r, attempt_q, db)
    db.commit()
    return demo_status(attempt, db)


@router.post("/{session_token}/previous")
def previous_question(session_token: str, db: Session = Depends(get_db), owner: User = Depends(verify_owner_access)):
    """Classic Zakovat only (Svoya Igra has no order: any cell can be reopened from the board)."""
    attempt = load_owned_demo(session_token, owner, db)
    if _game_mode(attempt) == "svoyak":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Svoya Igrada savolni doskadan tanlang")
    rounds = _rounds(attempt, db)
    order = _flat_positions(rounds)
    here = (attempt.current_round_index, attempt.current_question_index)
    prev = max(0, order.index(here) - 1) if here in order else 0
    open_question(attempt, rounds, *order[prev], db)
    db.commit()
    return demo_status(attempt, db)


@router.post("/{session_token}/reveal")
def reveal_answer(session_token: str, db: Session = Depends(get_db), owner: User = Depends(verify_owner_access)):
    """Shows the current question's answer and explanation to the owner. Read-only: no answer is recorded,
    the clock and the score do not change."""
    attempt = load_owned_demo(session_token, owner, db)
    rounds = _rounds(attempt, db)
    question = _question_at(rounds, attempt.current_round_index, attempt.current_question_index)
    accepted = [a.get("answer_text") for a in (question.get("accepted_answers") or []) if a.get("answer_text")]
    return {
        "round_index": attempt.current_round_index,
        "question_index": attempt.current_question_index,
        "correct_answer": resolve_primary_answer(question),
        "accepted_answers": accepted,
        "explanation": clean_explanation(question.get("explanation")),
    }


@router.post("/{session_token}/timer")
def control_timer(session_token: str, payload: TimerRequest, db: Session = Depends(get_db),
                  owner: User = Depends(verify_owner_access)):
    """Moves the question's server-side start time (the only clock there is), so every reader — the page,
    a reload, another device — sees the same remaining time. Demos are untimed for scoring purposes
    (preview class), so no control here can change a result."""
    attempt = load_owned_demo(session_token, owner, db)
    rounds = _rounds(attempt, db)
    limit = _question_limit(attempt, rounds)
    if not limit or not _question_is_open(attempt):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Hozir ochiq vaqtli savol yo‘q")
    meta = _meta(attempt)
    now = _now()
    opened = _aware(attempt.question_opened_at)
    paused_at = _aware(datetime.fromisoformat(meta["demo_paused_at"])) if meta.get("demo_paused_at") else None
    action = payload.action

    if action == "pause":
        if not paused_at:
            meta["demo_paused_at"] = now.isoformat()
    elif action == "resume":
        if paused_at:
            attempt.question_opened_at = opened + (now - paused_at)      # the paused span never counted
            meta.pop("demo_paused_at", None)
    elif action == "add":
        step = max(-MAX_TIME_STEP, min(MAX_TIME_STEP, int(payload.seconds)))
        reference = paused_at or now
        elapsed = (reference - opened).total_seconds()
        # remaining = limit - elapsed; adding time moves the start later, never past the reference point
        new_elapsed = min(max(elapsed - step, -MAX_TIME_STEP), float(limit))
        attempt.question_opened_at = reference - timedelta(seconds=new_elapsed)
    elif action == "reset":
        attempt.question_opened_at = now
        meta.pop("demo_paused_at", None)
    elif action == "expire":
        # Time is up now; the page then runs its normal time-out path (one blank answer, 0 points).
        attempt.question_opened_at = now - timedelta(seconds=limit)
        meta.pop("demo_paused_at", None)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Noma’lum taymer amali")
    _save_meta(attempt, meta)
    db.commit()
    return demo_status(attempt, db)


@router.post("/{session_token}/score")
def control_score(session_token: str, payload: ScoreRequest, db: Session = Depends(get_db),
                  owner: User = Depends(verify_owner_access)):
    """Manual score changes, kept as a separate adjustment and flagged as modified."""
    attempt = load_owned_demo(session_token, owner, db)
    meta = _meta(attempt)
    if payload.action == "add":
        step = max(-MAX_SCORE_STEP, min(MAX_SCORE_STEP, int(payload.delta)))
        meta["demo_score_adjustment"] = int(meta.get("demo_score_adjustment", 0)) + step
    elif payload.action == "reset":
        # Back to zero whatever the answers earned: the adjustment cancels them out (still flagged).
        records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
        if _game_mode(attempt) == "svoyak":
            meta["demo_score_adjustment"] = -sum(int(r.points_awarded or 0) for r in records)
        else:
            meta["demo_score_adjustment"] = -sum(1 for r in records if r.is_correct)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Noma’lum ball amali")
    meta["demo_score_modified"] = True
    _save_meta(attempt, meta)
    recompute_totals(attempt, db)
    db.commit()
    return demo_status(attempt, db)


@router.post("/{session_token}/end")
def end_demo(session_token: str, db: Session = Depends(get_db), owner: User = Depends(verify_owner_access)):
    attempt = load_owned_demo(session_token, owner, db)
    finish(attempt, db)
    db.commit()
    return demo_status(attempt, db)


@router.post("/{session_token}/restart")
def restart_demo(session_token: str, db: Session = Depends(get_db), owner: User = Depends(verify_owner_access)):
    """Same session, from the beginning: answers, score and adjustments cleared."""
    attempt = load_owned_demo(session_token, owner, db)
    reset_demo_in_place(attempt, _rounds(attempt, db), db)
    db.commit()
    return demo_status(attempt, db)


def reset_demo_in_place(attempt: SoloAttempt, rounds: List[Dict[str, Any]], db: Session) -> None:
    """The one demo reset, shared by the Owner Controls restart and the game header's ordinary
    restart button (api.play.restart_play_session). The session stays the same owner-only demo:
    answers, progress, score, manual adjustment and its flag, pause and break state are cleared."""
    db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).delete(synchronize_session=False)
    meta = _meta(attempt)
    for key in ("demo_score_adjustment", "demo_score_modified", "demo_paused_at", "awaiting_continue",
                "break_started_at", "break_duration_seconds", "expired_at"):
        meta.pop(key, None)
    meta["is_demo"] = True                    # stays an owner demo (and never competitive)
    meta["competitive"] = False
    _save_meta(attempt, meta)
    attempt.active_time_seconds = 0
    attempt.completed_at = None
    attempt.started_at = _now()
    if _game_mode(attempt) == "svoyak":
        attempt.current_round_index = 0
        attempt.current_question_index = 0
        back_to_board(attempt)
        recompute_totals(attempt, db)
    else:
        open_question(attempt, rounds, 0, 0, db)
