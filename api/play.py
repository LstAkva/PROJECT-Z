import string
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer, SoloAttempt, AnswerRecord
import re
router = APIRouter(prefix="/api/play", tags=["play"])


class AnswerSubmission(BaseModel):
    answer: str


def normalize_uzbek_latin(text: str) -> str:
    """Normalizes specifically for Uzbek Latin ZakoWhat content."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[’ʻʼ`]", "'", text)
    text = text.strip('.,!?"()[]{}:;* ')
    text = re.sub(r'\s+', ' ', text)
    return text


def get_ordered_rounds(db: Session, quiz_version_id: int) -> List[Round]:
    return (
        db.query(Round)
        .filter(Round.quiz_version_id == quiz_version_id)
        .order_by(Round.sequence.asc())
        .all()
    )


def get_round_questions(db: Session, round_id: int) -> List[Question]:
    return (
        db.query(Question)
        .filter(Question.round_id == round_id)
        .order_by(Question.sequence.asc())
        .all()
    )


def format_question_response(question: Question, question_index: int, total_in_round: int, round_obj: Round) -> dict:
    return {
        "status": "in_progress",
        "round": {
            "id": round_obj.id,
            "sequence": round_obj.sequence,
            "round_type": round_obj.round_type,
        },
        "question": {
            "id": question.id,
            "sequence": question.sequence,
            "text": question.text,
            "media_provider": question.media_provider,
            "media_url": question.media_url,
            "points": question.points,
        },
        "round_question_index": question_index,
        "round_total_questions": total_in_round,
    }


@router.post("/start/{quiz_id}", status_code=status.HTTP_201_CREATED)
def start_solo_attempt(quiz_id: int, db: Session = Depends(get_db)):
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




    rounds = get_ordered_rounds(db, latest_published.id)
    if not rounds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quiz has no rounds")

    first_round = rounds[0]
    questions = get_round_questions(db, first_round.id)
    if not questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="First round has no questions")

    attempt = SoloAttempt(
        quiz_version_id=latest_published.id,
        current_round_index=0,
        current_question_index=0,
        total_score=0,
        status="in_progress",
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return {
        "session_token": attempt.session_token,
        "quiz_title": quiz.title,
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

    rounds = get_ordered_rounds(db, attempt.quiz_version_id)
    current_round = rounds[attempt.current_round_index]
    questions = get_round_questions(db, current_round.id)
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

    rounds = get_ordered_rounds(db, attempt.quiz_version_id)
    current_round = rounds[attempt.current_round_index]
    questions = get_round_questions(db, current_round.id)
    current_q = questions[attempt.current_question_index]

    accepted_answers = db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == current_q.id).all()
    cleaned_input = normalize_uzbek_latin(submission.answer)
    
    is_correct = False
    matched_answer_text = None
    
    # Проверка на правильность
    for ans in accepted_answers:
        norm_ans = normalize_uzbek_latin(ans.answer_text)
        if norm_ans == cleaned_input:
            is_correct = True
            matched_answer_text = norm_ans
            break

    # Правило Zanjir: ответ должен начинаться на последнюю букву предыдущего ПРАВИЛЬНОГО ответа
    if is_correct and current_round.round_type == "zanjir" and attempt.current_question_index > 0:
        prev_q = questions[attempt.current_question_index - 1]
        prev_accepted = db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == prev_q.id).first()
        
        if prev_accepted:
            prev_norm = normalize_uzbek_latin(prev_accepted.answer_text)
            expected_start = prev_norm[-1] if prev_norm else ""
            actual_start = matched_answer_text[0] if matched_answer_text else ""
            
            if expected_start != actual_start:
                is_correct = False

    points = current_q.points if is_correct else 0

    record = AnswerRecord(
        attempt_id=attempt.id,
        question_id=current_q.id,
        submitted_text=submission.answer,
        is_correct=is_correct,
        points_awarded=points,
    )
    db.add(record)
    attempt.total_score += points
    attempt.current_question_index += 1

    # Проверяем, завершился ли текущий раунд
    if attempt.current_question_index >= len(questions):
        attempt.status = "round_reveal"
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

    rounds = get_ordered_rounds(db, attempt.quiz_version_id)
    # The round to reveal is the one the player just finished
    target_round = rounds[attempt.current_round_index]
    questions = get_round_questions(db, target_round.id)

    # Fetch player's answer records for these questions
    question_ids = [q.id for q in questions]
    records = {
        rec.question_id: rec
        for rec in db.query(AnswerRecord)
        .filter(AnswerRecord.attempt_id == attempt.id, AnswerRecord.question_id.in_(question_ids))
        .all()
    }

    revealed_questions = []
    round_score = 0

    for q in questions:
        rec = records.get(q.id)
        accepted = db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q.id).all()
        correct_answers = [a.answer_text for a in accepted]

        points = rec.points_awarded if rec else 0
        round_score += points

        revealed_questions.append({
            "question_id": q.id,
            "sequence": q.sequence,
            "text": q.text,
            "submitted_answer": rec.submitted_text if rec else None,
            "is_correct": rec.is_correct if rec else False,
            "points_awarded": points,
            "correct_answers": correct_answers,
        })

    is_last_round = attempt.current_round_index >= (len(rounds) - 1)

    return {
        "round_id": target_round.id,
        "round_sequence": target_round.sequence,
        "round_type": target_round.round_type,
        "round_score": round_score,
        "total_score_so_far": attempt.total_score,
        "config": target_round.config or {},  # Now safely reveals Mantiqasqon hidden_rule
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

    rounds = get_ordered_rounds(db, attempt.quiz_version_id)
    is_last_round = attempt.current_round_index >= (len(rounds) - 1)

    if is_last_round:
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "status": "completed",
            "quiz_completed": True,
            "total_score": attempt.total_score,
            "message": "Quiz completed! Fetch /results for summary.",
        }

    attempt.current_round_index += 1
    attempt.current_question_index = 0
    attempt.status = "in_progress"
    db.commit()

    next_round = rounds[attempt.current_round_index]
    next_questions = get_round_questions(db, next_round.id)

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

    rounds = get_ordered_rounds(db, attempt.quiz_version_id)
    all_records = db.query(AnswerRecord).filter(AnswerRecord.attempt_id == attempt.id).all()
    record_map = {r.question_id: r for r in all_records}

    round_summaries = []
    total_correct = 0
    total_incorrect = 0

    for r in rounds:
        questions = get_round_questions(db, r.id)
        r_score = 0
        r_correct = 0
        r_incorrect = 0

        for q in questions:
            rec = record_map.get(q.id)
            if rec and rec.is_correct:
                r_correct += 1
                r_score += rec.points_awarded
            else:
                r_incorrect += 1

        total_correct += r_correct
        total_incorrect += r_incorrect

        round_summaries.append({
            "round_id": r.id,
            "round_sequence": r.sequence,
            "round_type": r.round_type,
            "round_score": r_score,
            "correct_count": r_correct,
            "incorrect_count": r_incorrect,
        })

    return {
        "status": "completed",
        "total_score": attempt.total_score,
        "total_correct": total_correct,
        "total_incorrect": total_incorrect,
        "started_at": attempt.started_at.isoformat(),
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
        "rounds": round_summaries,
    }