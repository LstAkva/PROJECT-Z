import string
import secrets
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, AnswerRecord, User
from services.gameplay import normalize_uzbek_latin, verify_zanjir_chain, is_true_false_match
from api.auth import get_current_user_optional, is_secure_cookie

router = APIRouter(prefix="/api/play", tags=["play"])


class AnswerSubmission(BaseModel):
    answer: str
    is_wager: bool = False


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
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )
    result = []
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

        result.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "config": r.config or {},
            "questions": questions_data,
        })
    return result


def format_question_response(
    q_data: Dict[str, Any],
    question_index: int,
    total_in_round: int,
    round_data: Dict[str, Any],
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

    return {
        "status": "in_progress",
        "round": {
            "id": round_data.get("round_id"),
            "sequence": round_data.get("sequence"),
            "round_type": round_data.get("round_type"),
            "config": sanitize_config(round_data.get("config", {})),
        },
        "question": {
            "id": q_data.get("question_id"),
            "round_question_id": q_data.get("round_question_id"),
            "sequence": q_data.get("sequence"),
            "text": q_data.get("text"),
            "media_provider": q_data.get("media_provider"),
            "media_url": q_data.get("media_url"),
            "question_type": q_data.get("question_type", "text"),
            "options": safe_options,
            "points": q_data.get("points", 1),
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
        anon_cookie = request.cookies.get("zakowhat_anon_id")
        anon_header = request.headers.get("X-Anon-Id")
        # Per Correction 2: Cookie is authoritative in browser context. Header only if no cookie (API/test clients).
        anon_id = anon_cookie or anon_header
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

    now = datetime.now(timezone.utc)
    attempt = SoloAttempt(
        quiz_version_id=latest_published.id,
        user_id=user_id,
        anon_id=anon_id if not user_id else None,
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        total_correct=0,
        active_time_seconds=0,
        question_opened_at=now,
        status="in_progress",
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

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
        "current_state": format_question_response(questions[0], 0, len(questions), first_round),
    }


@router.get("/{session_token}", status_code=status.HTTP_200_OK)
def get_current_state(session_token: str, db: Session = Depends(get_db)):
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status == "completed":
        return {"status": "completed", "total_score": attempt.total_score}

    if attempt.status == "round_reveal":
        return {
            "status": "round_reveal",
            "message": "Round completed. Call /reveal to view answers and /continue to proceed.",
            "completed_round_index": attempt.current_round_index,
        }

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    current_round = rounds[attempt.current_round_index]
    questions = current_round.get("questions", [])
    current_q = questions[attempt.current_question_index]

    return format_question_response(current_q, attempt.current_question_index, len(questions), current_round)


@router.post("/{session_token}/answer", status_code=status.HTTP_200_OK)
def submit_answer(session_token: str, submission: AnswerSubmission, db: Session = Depends(get_db)):
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status == "completed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attempt is already completed")

    if attempt.status == "round_reveal":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current round is finished. Review the reveal and call /continue to start the next round."
        )

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)
    current_round = rounds[attempt.current_round_index]
    questions = current_round.get("questions", [])
    current_q = questions[attempt.current_question_index]

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
        # Check round-specific time limit if configured
        time_limit = current_round.get("config", {}).get("time_limit_seconds")
        if time_limit and float(time_limit) > 0:
            delta = min(delta, float(time_limit))
        time_taken = int(round(delta))
        attempt.active_time_seconds += time_taken

    meta = {"time_taken_seconds": time_taken}
    if is_wager:
        meta["is_wager"] = True
    if is_blank:
        meta["is_blank"] = True

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
        attempt.status = "round_reveal"
        attempt.question_opened_at = None  # Pauses active timer during reveal
        db.commit()

        is_last_round = attempt.current_round_index >= (len(rounds) - 1)
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
        "next_state": format_question_response(next_q, attempt.current_question_index, len(questions), current_round),
    }


@router.get("/{session_token}/reveal", status_code=status.HTTP_200_OK)
def get_round_reveal(session_token: str, db: Session = Depends(get_db)):
    """Exposes answers and rules ONLY for the round currently completed and waiting for reveal."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status not in ("round_reveal", "completed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reveal is not available until the round is fully answered."
        )

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
    target_round = rounds[attempt.current_round_index]
    questions = target_round.get("questions", [])

    # Fetch player's answer records for these questions
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
            "points_awarded": points,
            "correct_answers": correct_answers,
        })

    is_last_round = attempt.current_round_index >= (len(rounds) - 1)

    return {
        "round_id": target_round.get("round_id"),
        "round_sequence": target_round.get("sequence"),
        "round_type": target_round.get("round_type"),
        "round_score": round_score,
        "total_score_so_far": attempt.total_score,
        "config": target_round.get("config", {}),  # Now safely reveals Mantiqasqon hidden_rule
        "questions": revealed_questions,
        "has_next_round": not is_last_round,
    }


@router.post("/{session_token}/continue", status_code=status.HTTP_200_OK)
def continue_to_next_round(session_token: str, db: Session = Depends(get_db)):
    """Advances from round_reveal to the next round, or marks quiz completed if on the last round."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status != "round_reveal":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is not currently in a round reveal state."
        )

    rounds = get_manifest_or_db_rounds(attempt.quiz_version, db)
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
        "current_state": format_question_response(next_questions[0], 0, len(next_questions), next_round),
    }


@router.get("/{session_token}/results", status_code=status.HTTP_200_OK)
def get_final_results(session_token: str, db: Session = Depends(get_db)):
    """Returns the end-of-quiz score breakdown once the attempt is completed."""
    attempt = db.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if attempt.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Quiz results are only available after completing the attempt."
        )

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)
    all_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    record_map = {r.question_id: r for r in all_records}

    round_summaries = []
    total_correct = 0
    total_incorrect = 0

    for r in rounds:
        questions = r.get("questions", [])
        r_score = 0
        r_correct = 0
        r_incorrect = 0

        for q in questions:
            qid = q.get("question_id")
            rec = record_map.get(qid)
            if rec and rec.is_correct:
                r_correct += 1
                r_score += rec.points_awarded
            else:
                r_incorrect += 1
                if rec:
                    r_score += rec.points_awarded

        total_correct += r_correct
        total_incorrect += r_incorrect

        round_summaries.append({
            "round_id": r.get("round_id"),
            "round_sequence": r.get("sequence"),
            "round_type": r.get("round_type"),
            "round_score": r_score,
            "correct_count": r_correct,
            "incorrect_count": r_incorrect,
        })

    mins = attempt.active_time_seconds // 60
    secs = attempt.active_time_seconds % 60

    return {
        "status": "completed",
        "is_preview": (attempt.timer_mode == "preview"),
        "is_anonymous": (attempt.user_id is None),
        "game_mode": getattr(version, "game_mode", "modern_multiround"),
        "total_score": attempt.total_score,
        "total_correct": attempt.total_correct if attempt.total_correct else total_correct,
        "total_incorrect": total_incorrect,
        "total_questions": total_correct + total_incorrect,
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

    if attempt.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Javoblar tahlili faqat viktorina to'liq yakunlangandan keyin ko'rsatiladi."
        )

    version = attempt.quiz_version
    rounds = get_manifest_or_db_rounds(version, db)
    all_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    record_map = {r.question_id: r for r in all_records}

    review_rounds = []
    for r in rounds:
        questions_review = []
        for q in r.get("questions", []):
            qid = q.get("question_id")
            rec = record_map.get(qid)
            correct_answers = [a.get("answer_text") for a in q.get("accepted_answers", [])]
            primary_ans = next((a.get("answer_text") for a in q.get("accepted_answers", []) if a.get("is_primary")), None)
            if not primary_ans and correct_answers:
                primary_ans = correct_answers[0]

            questions_review.append({
                "question_id": qid,
                "round_question_id": q.get("round_question_id"),
                "sequence": q.get("sequence"),
                "text": q.get("text"),
                "question_type": q.get("question_type", "text"),
                "options": q.get("options"),
                "explanation": q.get("explanation"),
                "submitted_answer": rec.submitted_text if rec else None,
                "is_correct": rec.is_correct if rec else False,
                "points_awarded": rec.points_awarded if rec else 0,
                "time_taken_seconds": (rec.record_metadata or {}).get("time_taken_seconds", 0) if rec else 0,
                "primary_answer": primary_ans,
                "correct_answers": correct_answers,
            })

        review_rounds.append({
            "round_id": r.get("round_id"),
            "round_sequence": r.get("sequence"),
            "round_type": r.get("round_type"),
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
        "total_correct": attempt.total_correct,
        "active_time_seconds": attempt.active_time_seconds,
        "formatted_time": f"{mins:02d}:{secs:02d}",
        "rounds": review_rounds,
    }