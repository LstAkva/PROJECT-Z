"""
Jonli xona (live rooms): a host-led game that teams join with a short code.

* A host (the owner, or an email listed in HOST_EMAILS) creates a room for a published Zakovat-format pack and runs it: start, reveal the
  answer, next question, pause/resume the clock, overrule the automatic check, remove a team, end.
* Teams join at /join with the code and a team name. No account: the device keeps a secret token
  (sent as X-Team-Token; stored only as a SHA-256 hash).
* Everyone answers the same question at the same time; one point per correct answer. The projector page shows
  the question, the clock, how many teams answered, then the answer and the table.
* Rooms never touch rankings, statistics or player progress. Rooms (with teams and answers) are deleted
  30 days after creation.
"""
import csv
import hashlib
import io
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.auth import enforce_rate_limit, get_current_user_required, get_owner_emails
from api.play import clean_explanation, get_manifest_or_db_rounds, resolve_primary_answer
from database import get_db
from models import LiveAnswer, LiveRoom, LiveTeam, Quiz, QuizVersion, User
from services.gameplay import is_conservative_typo_match, is_true_false_match, normalize_uzbek_latin

router = APIRouter(prefix="/api/rooms", tags=["rooms"])

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: easy to read aloud and type
CODE_LENGTH = 6
MAX_TEAMS = 60
RETENTION_DAYS = 30
ANSWER_GRACE_SECONDS = 1                              # network latency at the last second
NO_STORE = {"Cache-Control": "no-store"}
ROOM_NOT_FOUND = "Xona topilmadi. Kodni tekshiring."
JOIN_DISABLED = "Telefon orqali qo‘shilish hozircha yoqilmagan"


def get_host_emails() -> List[str]:
    """Hosts (e.g. a professor running a tournament): may create and run rooms, nothing else of the owner panel."""
    return [e.strip().lower() for e in os.getenv("HOST_EMAILS", "").split(",") if e.strip()]


def is_room_host(user: Optional[User]) -> bool:
    if not user or not user.email:
        return False
    return user.email.lower() in set(get_owner_emails()) | set(get_host_emails())   # fails closed when both are empty


def verify_room_host(current_user: User = Depends(get_current_user_required)) -> User:
    if not is_room_host(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Jonli xonani faqat boshlovchi ochishi mumkin")
    return current_user


def player_join_enabled() -> bool:
    """Teams joining from their phones (/join). Off by default: until the server is load-tested, rooms run in
    presentation mode (host console + projector screen; teams answer on paper). ROOMS_PLAYER_JOIN=1 turns it on."""
    return os.getenv("ROOMS_PLAYER_JOIN", "").strip().lower() in ("1", "true", "yes", "on")


def _require_join_enabled() -> None:
    if not player_join_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOIN_DISABLED)


# ----------------------------------------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())[:CODE_LENGTH + 2]


def _new_code(db: Session) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not db.query(LiveRoom.id).filter(LiveRoom.code == code).first():
            return code
    raise HTTPException(status_code=503, detail="Kod yaratib bo‘lmadi, qayta urinib ko‘ring")


def _delete_expired_rooms(db: Session) -> None:
    cutoff = _now() - timedelta(days=RETENTION_DAYS)
    for room in db.query(LiveRoom).filter(LiveRoom.created_at < cutoff.replace(tzinfo=None)).all():
        db.delete(room)          # teams and answers go with it (ORM cascade + ON DELETE CASCADE)


def _published_version(quiz: Quiz, db: Session) -> Optional[QuizVersion]:
    return (db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
            .order_by(QuizVersion.version_number.desc()).first())


def room_questions(version: QuizVersion, db: Session) -> List[Dict[str, Any]]:
    """The pack's questions in playing order, flattened across rounds."""
    flat = []
    for r in get_manifest_or_db_rounds(version, db) or []:
        for q in r.get("questions") or []:
            flat.append({
                "round_name": r.get("name") or "",
                "round_type": r.get("round_type") or "",
                "text": q.get("text") or "",
                "options": q.get("options") if isinstance(q.get("options"), list) else None,
                "media_url": q.get("media_url"),
                "question_type": q.get("question_type") or "text",
                "accepted_answers": q.get("accepted_answers") or [],
                "correct_answer": resolve_primary_answer(q),
                "explanation": clean_explanation(q.get("explanation")),
            })
    return flat


def grade_answer(q: Dict[str, Any], submitted: str) -> bool:
    """Same matching as solo play: normalized exact match, option keys for multiple choice, true/false,
    then conservative typo tolerance."""
    cleaned = normalize_uzbek_latin(submitted or "")
    if not cleaned:
        return False
    accepted = [normalize_uzbek_latin(a.get("answer_text", "")) for a in q.get("accepted_answers") or []]
    accepted = [a for a in accepted if a]
    if not accepted and q.get("correct_answer"):
        accepted = [normalize_uzbek_latin(q["correct_answer"])]
    options = q.get("options")
    if options and (q.get("question_type") == "mcq" or q.get("round_type") in ("gulmisiz", "gulmisiz_rayhonmisiz", "mcq")):
        chosen = next((o for o in options if str(o.get("key", "")).upper() == submitted.strip().upper()), None)
        if chosen:
            keys = {normalize_uzbek_latin(str(chosen.get("key", ""))), normalize_uzbek_latin(str(chosen.get("text", "")))}
            return any(a in keys for a in accepted)
    if q.get("round_type") in ("true_false", "aldama_meni"):
        return any(is_true_false_match(cleaned, a) for a in accepted)
    if cleaned in accepted:
        return True
    return any(is_conservative_typo_match(cleaned, a) for a in accepted)


def _remaining(room: LiveRoom) -> Optional[int]:
    if room.status != "question":
        return None
    if room.paused_remaining is not None:
        return max(0, room.paused_remaining)
    deadline = _aware(room.question_deadline)
    if deadline is None:
        return None
    return max(0, math.ceil((deadline - _now()).total_seconds()))


def _answer_window_open(room: LiveRoom) -> bool:
    if room.status != "question":
        return False
    if room.paused_remaining is not None:
        return room.paused_remaining > 0
    deadline = _aware(room.question_deadline)
    return deadline is None or _now() <= deadline + timedelta(seconds=ANSWER_GRACE_SECONDS)


def _open_question(room: LiveRoom, index: int) -> None:
    room.status = "question"
    room.current_index = index
    room.paused_remaining = None
    room.question_deadline = (_now() + timedelta(seconds=room.seconds_per_question)).replace(tzinfo=None)


def _standings(room: LiveRoom, db: Session) -> List[Dict[str, Any]]:
    teams = [t for t in room.teams if not t.removed]
    points = {t.id: 0 for t in teams}
    for a in db.query(LiveAnswer).filter(LiveAnswer.room_id == room.id).all():
        if a.team_id in points and a.is_correct:
            points[a.team_id] += 1
    ordered = sorted(teams, key=lambda t: (-points[t.id], t.joined_at or _now().replace(tzinfo=None), t.id))
    table, last_points, rank = [], None, 0
    for position, t in enumerate(ordered, start=1):
        if points[t.id] != last_points:
            rank, last_points = position, points[t.id]
        table.append({"team_id": t.id, "name": t.name, "points": points[t.id], "rank": rank})
    return table


def _load_room(code: str, db: Session, lock: bool = False) -> LiveRoom:
    query = db.query(LiveRoom).filter(LiveRoom.code == _normalize_code(code))
    if lock:
        query = query.with_for_update()
    room = query.first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ROOM_NOT_FOUND)
    return room


def _hosted_room(code: str, owner: User, db: Session, lock: bool = False) -> LiveRoom:
    room = _load_room(code, db, lock=lock)
    if room.host_user_id != owner.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ROOM_NOT_FOUND)
    return room


def _team_for(room: LiveRoom, token: Optional[str], db: Session) -> LiveTeam:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Avval xonaga qo‘shiling")
    team = db.query(LiveTeam).filter(LiveTeam.token_hash == _hash_token(token), LiveTeam.room_id == room.id).first()
    if not team:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Avval xonaga qo‘shiling")
    if team.removed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Boshlovchi jamoangizni xonadan chiqardi")
    return team


def _context(room: LiveRoom, db: Session):
    version = db.query(QuizVersion).filter(QuizVersion.id == room.quiz_version_id).first()
    quiz = db.query(Quiz).filter(Quiz.id == version.quiz_id).first() if version else None
    questions = room_questions(version, db) if version else []
    return (quiz.title if quiz else ""), questions


def _public_question(q: Dict[str, Any]) -> Dict[str, Any]:
    return {"round_name": q["round_name"], "text": q["text"], "media_url": q["media_url"],
            "options": [{"key": o.get("key"), "text": o.get("text")} for o in (q["options"] or [])] or None}


def _base_state(room: LiveRoom, title: str, questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = {
        "code": room.code, "status": room.status, "pack_title": title,
        "question_number": room.current_index + 1 if room.current_index >= 0 else 0,
        "total_questions": len(questions),
        "seconds_per_question": room.seconds_per_question,
        "remaining_seconds": _remaining(room),
        "paused": room.paused_remaining is not None and room.status == "question",
        "question": None, "reveal": None,
    }
    if room.status in ("question", "reveal") and 0 <= room.current_index < len(questions):
        q = questions[room.current_index]
        state["question"] = _public_question(q)
        if room.status == "reveal":
            state["reveal"] = {"correct_answer": q["correct_answer"], "explanation": q["explanation"]}
    return state


# ----------------------------------------------------------------------------------------------- host

class CreateRoomRequest(BaseModel):
    quiz_id: int
    seconds_per_question: int = Field(60, ge=10, le=300)


class TimerRequest(BaseModel):
    action: str = Field(..., pattern="^(pause|resume|add)$")
    seconds: int = Field(15, ge=5, le=120)


class VerdictRequest(BaseModel):
    correct: Optional[bool] = None          # None restores the automatic check


@router.get("/packs")
def host_packs(response: Response, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    """Published Zakovat-format packs a host can run (Svoya Igra is not supported in rooms yet)."""
    response.headers.update(NO_STORE)
    out = []
    for quiz in db.query(Quiz).order_by(Quiz.id.desc()).all():
        version = _published_version(quiz, db)
        if not version or version.game_mode == "svoyak":
            continue
        count = sum(len(r.get("questions") or []) for r in (version.published_manifest or {}).get("rounds", []))
        if count:
            out.append({"quiz_id": quiz.id, "title": quiz.title, "question_count": count})
    return {"packs": out}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_room(payload: CreateRoomRequest, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    quiz = db.query(Quiz).filter(Quiz.id == payload.quiz_id).first()
    version = _published_version(quiz, db) if quiz else None
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nashr qilingan to‘plam topilmadi")
    if version.game_mode == "svoyak":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Jonli xona hozircha faqat Zakovat formatidagi to‘plamlar uchun")
    if not room_questions(version, db):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="To‘plamda savollar yo‘q")
    _delete_expired_rooms(db)
    room = LiveRoom(code=_new_code(db), host_user_id=owner.id, quiz_version_id=version.id,
                    seconds_per_question=payload.seconds_per_question, status="lobby", current_index=-1)
    db.add(room)
    db.commit()
    return {"code": room.code, "host_url": f"/room/{room.code}/host", "screen_url": f"/room/{room.code}/screen",
            "join_url": f"/join?code={room.code}"}


@router.get("/mine")
def my_rooms(response: Response, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    response.headers.update(NO_STORE)
    rooms = (db.query(LiveRoom).filter(LiveRoom.host_user_id == owner.id)
             .order_by(LiveRoom.created_at.desc()).limit(20).all())
    out = []
    for room in rooms:
        version = db.query(QuizVersion).filter(QuizVersion.id == room.quiz_version_id).first()
        quiz = db.query(Quiz).filter(Quiz.id == version.quiz_id).first() if version else None
        out.append({"code": room.code, "status": room.status, "pack_title": quiz.title if quiz else "",
                    "teams": sum(1 for t in room.teams if not t.removed),
                    "created_at": _aware(room.created_at).isoformat()})
    return {"rooms": out}


def host_state(room: LiveRoom, db: Session) -> Dict[str, Any]:
    title, questions = _context(room, db)
    state = _base_state(room, title, questions)
    state["join_url"] = f"/join?code={room.code}"
    state["player_join"] = player_join_enabled()
    if 0 <= room.current_index < len(questions):
        q = questions[room.current_index]
        state["host_question"] = {"correct_answer": q["correct_answer"], "explanation": q["explanation"],
                                  "accepted_answers": [a.get("answer_text") for a in q["accepted_answers"]]}
    names = {t.id: t.name for t in room.teams}
    answers = (db.query(LiveAnswer).filter(LiveAnswer.room_id == room.id,
                                           LiveAnswer.question_index == room.current_index)
               .order_by(LiveAnswer.answered_at.asc()).all()) if room.current_index >= 0 else []
    active_ids = {t.id for t in room.teams if not t.removed}
    state["answers"] = [{"id": a.id, "team": names.get(a.team_id, "?"), "text": a.submitted_text,
                         "auto_correct": a.auto_correct, "host_verdict": a.host_verdict, "correct": a.is_correct}
                        for a in answers if a.team_id in active_ids]
    state["answered_count"] = len(state["answers"])
    state["teams"] = [{"id": t.id, "name": t.name} for t in room.teams if not t.removed]
    state["standings"] = _standings(room, db)
    return state


@router.get("/{code}/host")
def get_host_state(code: str, response: Response, db: Session = Depends(get_db),
                   owner: User = Depends(verify_room_host)):
    response.headers.update(NO_STORE)
    return host_state(_hosted_room(code, owner, db), db)


def _host_action(code: str, owner: User, db: Session, action) -> Dict[str, Any]:
    room = _hosted_room(code, owner, db, lock=True)
    _, questions = _context(room, db)
    action(room, questions)
    db.commit()
    db.refresh(room)
    return host_state(room, db)


def _conflict(message: str):
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


@router.post("/{code}/start")
def start_room(code: str, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    def act(room, questions):
        if room.status != "lobby":
            _conflict("O‘yin allaqachon boshlangan")
        _open_question(room, 0)
    return _host_action(code, owner, db, act)


@router.post("/{code}/reveal")
def reveal_room_answer(code: str, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    def act(room, questions):
        if room.status != "question":
            _conflict("Hozir ochiq savol yo‘q")
        room.status = "reveal"
        room.question_deadline = None
        room.paused_remaining = None
    return _host_action(code, owner, db, act)


@router.post("/{code}/next")
def next_room_question(code: str, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    def act(room, questions):
        if room.status != "reveal":
            _conflict("Avval javobni oching")
        if room.current_index + 1 >= len(questions):
            room.status = "finished"
            room.finished_at = _now().replace(tzinfo=None)
        else:
            _open_question(room, room.current_index + 1)
    return _host_action(code, owner, db, act)


@router.post("/{code}/timer")
def control_room_timer(code: str, payload: TimerRequest, db: Session = Depends(get_db),
                       owner: User = Depends(verify_room_host)):
    def act(room, questions):
        if room.status != "question":
            _conflict("Hozir ochiq savol yo‘q")
        left = _remaining(room) or 0
        if payload.action == "pause":
            room.paused_remaining = left
            room.question_deadline = None
        elif payload.action == "resume":
            if room.paused_remaining is not None:
                room.question_deadline = (_now() + timedelta(seconds=room.paused_remaining)).replace(tzinfo=None)
                room.paused_remaining = None
        else:  # add time
            if room.paused_remaining is not None:
                room.paused_remaining += payload.seconds
            else:
                room.question_deadline = (_now() + timedelta(seconds=left + payload.seconds)).replace(tzinfo=None)
    return _host_action(code, owner, db, act)


@router.post("/{code}/answers/{answer_id}/verdict")
def set_answer_verdict(code: str, answer_id: int, payload: VerdictRequest, db: Session = Depends(get_db),
                       owner: User = Depends(verify_room_host)):
    def act(room, questions):
        answer = db.query(LiveAnswer).filter(LiveAnswer.id == answer_id, LiveAnswer.room_id == room.id).first()
        if not answer:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Javob topilmadi")
        answer.host_verdict = payload.correct
    return _host_action(code, owner, db, act)


@router.post("/{code}/teams/{team_id}/remove")
def remove_team(code: str, team_id: int, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    def act(room, questions):
        team = db.query(LiveTeam).filter(LiveTeam.id == team_id, LiveTeam.room_id == room.id).first()
        if not team:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Jamoa topilmadi")
        team.removed = True
    return _host_action(code, owner, db, act)


@router.post("/{code}/end")
def end_room(code: str, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    def act(room, questions):
        room.status = "finished"
        room.question_deadline = None
        room.paused_remaining = None
        room.finished_at = room.finished_at or _now().replace(tzinfo=None)
    return _host_action(code, owner, db, act)


@router.get("/{code}/export")
def export_room(code: str, db: Session = Depends(get_db), owner: User = Depends(verify_room_host)):
    """Final table for the organisers: one row per team, 1/0 per question, total."""
    room = _hosted_room(code, owner, db)
    _, questions = _context(room, db)
    marks: Dict[int, Dict[int, int]] = {}
    for a in db.query(LiveAnswer).filter(LiveAnswer.room_id == room.id).all():
        marks.setdefault(a.team_id, {})[a.question_index] = 1 if a.is_correct else 0
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["O‘rin", "Jamoa", "Ball"] + [f"S{i + 1}" for i in range(len(questions))])
    for row in _standings(room, db):
        team_marks = marks.get(row["team_id"], {})
        name = row["name"]
        if name[:1] in ("=", "+", "-", "@"):
            name = "'" + name                      # keep spreadsheet apps from running it as a formula
        writer.writerow([row["rank"], name, row["points"]] + [team_marks.get(i, 0) for i in range(len(questions))])
    return Response(content="﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={**NO_STORE, "Content-Disposition": f'attachment; filename="zakowhat-xona-{room.code}.csv"'})


# ----------------------------------------------------------------------------------------------- players

class JoinRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=12)
    team_name: str = Field(..., min_length=1, max_length=60)


class AnswerRequest(BaseModel):
    text: str = Field(..., max_length=300)


@router.post("/join", status_code=status.HTTP_201_CREATED)
def join_room(payload: JoinRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    _require_join_enabled()
    enforce_rate_limit(request, "room_join", max_requests=20, window_seconds=60)
    response.headers.update(NO_STORE)
    room = _load_room(payload.code, db, lock=True)
    if room.status == "finished":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bu xonadagi o‘yin tugagan")
    name = " ".join(payload.team_name.split())[:40]
    if not name:
        raise HTTPException(status_code=422, detail="Jamoa nomini yozing")
    if sum(1 for t in room.teams if not t.removed) >= MAX_TEAMS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Xona to‘lgan")
    key = name.casefold()[:40]
    if db.query(LiveTeam.id).filter(LiveTeam.room_id == room.id, LiveTeam.name_key == key).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bu nomdagi jamoa allaqachon bor, boshqa nom tanlang")
    token = secrets.token_urlsafe(24)
    team = LiveTeam(room_id=room.id, name=name, name_key=key, token_hash=_hash_token(token))
    db.add(team)
    db.commit()
    return {"token": token, "team": {"id": team.id, "name": team.name}, "code": room.code}


@router.get("/{code}/me")
def player_state(code: str, response: Response, db: Session = Depends(get_db),
                 x_team_token: Optional[str] = Header(None)):
    _require_join_enabled()
    response.headers.update(NO_STORE)
    room = _load_room(code, db)
    team = _team_for(room, x_team_token, db)
    title, questions = _context(room, db)
    state = _base_state(room, title, questions)
    mine = None
    if room.current_index >= 0:
        mine = db.query(LiveAnswer).filter(LiveAnswer.team_id == team.id,
                                           LiveAnswer.question_index == room.current_index).first()
    state["team"] = {"id": team.id, "name": team.name}
    state["my_answer"] = mine.submitted_text if mine else None
    state["answer_open"] = _answer_window_open(room)
    if room.status == "reveal":
        state["my_correct"] = bool(mine and mine.is_correct)
    table = _standings(room, db)
    me = next((row for row in table if row["team_id"] == team.id), None)
    state["my_points"] = me["points"] if me else 0
    state["my_rank"] = me["rank"] if me else None
    state["teams_count"] = len(table)
    state["standings"] = table[:10] if room.status in ("reveal", "finished") else None
    return state


@router.post("/{code}/answer")
def submit_room_answer(code: str, payload: AnswerRequest, request: Request, response: Response,
                       db: Session = Depends(get_db), x_team_token: Optional[str] = Header(None)):
    _require_join_enabled()
    enforce_rate_limit(request, "room_answer", max_requests=60, window_seconds=60)
    response.headers.update(NO_STORE)
    room = _load_room(code, db, lock=True)
    team = _team_for(room, x_team_token, db)
    if not _answer_window_open(room):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Javob qabul qilish vaqti tugadi")
    text = " ".join(payload.text.split())
    if not text:
        raise HTTPException(status_code=422, detail="Javobni yozing")
    _, questions = _context(room, db)
    correct = grade_answer(questions[room.current_index], text)
    answer = db.query(LiveAnswer).filter(LiveAnswer.team_id == team.id,
                                         LiveAnswer.question_index == room.current_index).first()
    if answer:                                   # the answer can be changed until time is up
        answer.submitted_text, answer.auto_correct, answer.host_verdict = text, correct, None
        answer.answered_at = _now().replace(tzinfo=None)
    else:
        db.add(LiveAnswer(room_id=room.id, team_id=team.id, question_index=room.current_index,
                          submitted_text=text, auto_correct=correct))
    db.commit()
    return {"saved": True, "my_answer": text}


# ----------------------------------------------------------------------------------------------- projector

@router.get("/{code}/screen")
def screen_state(code: str, response: Response, db: Session = Depends(get_db)):
    """Projector view: public to anyone with the code. Shows the correct answer only after the host reveals it."""
    response.headers.update(NO_STORE)
    room = _load_room(code, db)
    title, questions = _context(room, db)
    state = _base_state(room, title, questions)
    table = _standings(room, db)
    state["player_join"] = player_join_enabled()
    state["teams_count"] = len(table)
    state["teams"] = [row["name"] for row in sorted(table, key=lambda r: r["team_id"])] if room.status == "lobby" else None
    if room.status == "question" and state["player_join"]:
        active = {row["team_id"] for row in table}
        state["answered_count"] = (db.query(LiveAnswer)
                                   .filter(LiveAnswer.room_id == room.id, LiveAnswer.question_index == room.current_index,
                                           LiveAnswer.team_id.in_(active)).count()) if active else 0
    # In presentation mode nobody answers on the site, so there is no table to show.
    state["standings"] = table if room.status in ("reveal", "finished") and state["player_join"] else None
    return state


@router.get("/{code}/qr.svg")
def room_qr(code: str, db: Session = Depends(get_db)):
    """QR code of the join link for the projector screen (only while phone joining is on)."""
    _require_join_enabled()
    room = _load_room(code, db)
    import segno
    from services.daily_engagement import public_site_url
    qr = segno.make(f"{public_site_url()}/join?code={room.code}", error="m")
    out = io.BytesIO()
    qr.save(out, kind="svg", scale=10, border=2, dark="#1D181A", light="#FFFFFF", xmldecl=False, svgns=True)
    return Response(content=out.getvalue(), media_type="image/svg+xml", headers=NO_STORE)
