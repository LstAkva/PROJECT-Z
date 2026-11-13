from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload
from database import get_db
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, SoloAttempt, User
from api.auth import verify_owner_access, get_current_user_optional, public_identity, no_store_identity

router = APIRouter(prefix="/api/quizzes", tags=["quizzes"])

# Canonical agreed round types
CANONICAL_ROUND_TYPES = {
    "gulmisiz_rayhonmisiz": {
        "id": "gulmisiz_rayhonmisiz",
        "name": "1. Gulmisiz, rayhonmisiz?",
        "description": "Variantli savollar / To'g'ri-noto'g'ri tanlovi",
    },
    "zanjir": {
        "id": "zanjir",
        "name": "2. Zanjir",
        "description": "Har bir javob oldingi javobning oxirgi harfidan boshlanadi",
    },
    "mantiqqasqon": {
        "id": "mantiqqasqon",
        "name": "3. Mantiqqasqon",
        "description": "Savollar orasidagi yashirin mantiqiy bog'liqlik",
    },
    "aldama_meni": {
        "id": "aldama_meni",
        "name": "4. Aldama meni",
        "description": "Chalg'ituvchi faktlar va savollar",
    },
    "rasmiyatchilik": {
        "id": "rasmiyatchilik",
        "name": "5. Rasmiyatchilik",
        "description": "Aniq faktologiya va rasmiy ma'lumotlar",
    },
    "vabank": {
        "id": "vabank",
        "name": "6. Vabank",
        "description": "Tavakkal turi / Ballni oshirish yoki yo'qotish",
    },
    # Mode-specific canonical types:
    "svoyak_theme": {
        "id": "svoyak_theme",
        "name": "Svoяk Mavzusi",
        "description": "Svoyak mavzuli turlari (10, 20, 30, 40, 50 ball)",
    },
    "zakovat_classic": {
        "id": "zakovat_classic",
        "name": "Klassik Zakovat",
        "description": "Klassik 12+12 formatidagi savollar",
    },
    # Legacy / compatibility:
    "standard": {
        "id": "standard",
        "name": "Standart",
        "description": "Standart savollar turi",
    },
}


class DraftRoundQuestionItem(BaseModel):
    question_id: int
    sequence: Optional[int] = None
    points_override: Optional[int] = None
    config_override: Optional[Dict[str, Any]] = None


class DraftRoundItem(BaseModel):
    sequence: int
    round_type: str
    config: Optional[Dict[str, Any]] = None
    questions: Optional[List[DraftRoundQuestionItem]] = None
    question_ids: Optional[List[int]] = None


class CreateQuizDraftRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    game_mode: Optional[str] = "modern_multiround"
    round_type: Optional[str] = None
    round_config: Optional[Dict[str, Any]] = None
    question_ids: Optional[List[int]] = None
    rounds: Optional[List[DraftRoundItem]] = None



def sanitize_round_config(config: dict) -> dict:
    """Strips secret configuration keys (like hidden_rule) from public display."""
    if not config:
        return {}
    c = dict(config)
    c.pop("hidden_rule", None)
    return c


def compile_published_manifest(version: QuizVersion, db: Session) -> dict:
    """Compiles the complete immutable playable snapshot for a published quiz version."""
    db.flush()
    rounds_data = []
    rounds = (
        db.query(Round)
        .filter(Round.quiz_version_id == version.id)
        .order_by(Round.sequence.asc())
        .all()
    )

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
            # Fallback for legacy rounds without round_questions
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

        rounds_data.append({
            "round_id": r.id,
            "sequence": r.sequence,
            "round_type": r.round_type,
            "config": r.config or {},
            "questions": questions_data,
        })

    return {
        "version_id": version.id,
        "quiz_id": version.quiz_id,
        "title": version.quiz.title if version.quiz else None,
        "description": version.quiz.description if version.quiz else None,
        "version_number": version.version_number,
        "game_mode": version.game_mode or "modern_multiround",
        "category": (version.published_manifest or {}).get("category") or "Umumiy",
        "visibility": (version.published_manifest or {}).get("visibility", "public") if version.published_manifest else "public",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "rounds": rounds_data,
    }


# =========================================================
# LEADERBOARD COMPUTATION HELPER
def normalize_uzbek_text(text: Optional[str]) -> str:
    """Normalizes Uzbek apostrophe variants (ʻ, ‘, ’, `) to standard ASCII single quote for search/matching."""
    if not text:
        return ""
    return (
        text.lower()
        .replace("‘", "'")
        .replace("’", "'")
        .replace("ʻ", "'")
        .replace("`", "'")
    )


def fetch_version_leaderboard(version: QuizVersion, db: Session, viewer_id: Optional[int] = None) -> dict:
    """
    Computes per-quiz-version leaderboard:
    - Strictly scoped to this specific published version (never mixes across versions).
    - Only registered players (user_id is not None).
    - Only completed attempts (status == 'completed').
    - Excludes preview attempts.
    - Exactly ONE entry per player (new rule: their single ranked attempt; legacy: best attempt).
    - Ranking criteria:
        1. Zakovat/other modes: more correct answers first (total_correct DESC).
           Svoya Igra: higher final points first (total_score DESC).
        2. Lower active gameplay time first (active_time_seconds ASC).
        3. Earlier completion timestamp tie-breaker (completed_at ASC).
    """
    attempts = (
        db.query(SoloAttempt)
        .options(joinedload(SoloAttempt.user))
        .filter(
            SoloAttempt.quiz_version_id == version.id,
            SoloAttempt.status == "completed",
            SoloAttempt.user_id.isnot(None),
            SoloAttempt.timer_mode.notin_(["preview", "preview_no_timer"]),
        )
        .all()
    )

    # Ranked-attempt rule (one ranked opportunity per user per pack):
    # - Attempts created under the rule carry attempt_metadata["competitive"]. Only the user's single
    #   ranked attempt (earliest competitive=True across ALL versions of this pack) is eligible, and only
    #   if it was timed. Practice and no-timer attempts never enter or improve the leaderboard.
    # - Legacy attempts (created before the rule, no "competitive" key) keep their existing treatment.
    new_rule_users = {a.user_id for a in attempts if "competitive" in (a.attempt_metadata or {})}
    first_ranked_attempt_id: Dict[int, int] = {}
    if new_rule_users:
        pack_version_ids = [vid for (vid,) in db.query(QuizVersion.id).filter(QuizVersion.quiz_id == version.quiz_id)]
        rows = (
            db.query(SoloAttempt.id, SoloAttempt.user_id, SoloAttempt.attempt_metadata)
            .filter(SoloAttempt.user_id.in_(new_rule_users), SoloAttempt.quiz_version_id.in_(pack_version_ids))
            .all()
        )
        for att_id, uid, meta in rows:
            if (meta or {}).get("competitive") is True:
                if uid not in first_ranked_attempt_id or att_id < first_ranked_attempt_id[uid]:
                    first_ranked_attempt_id[uid] = att_id

    def is_leaderboard_eligible(att: SoloAttempt) -> bool:
        meta = att.attempt_metadata or {}
        if "competitive" not in meta:
            return True  # legacy result: unchanged treatment
        return (
            meta.get("competitive") is True
            and att.timer_mode in ("standard", "competitive")
            and first_ranked_attempt_id.get(att.user_id) == att.id
        )

    attempts = [a for a in attempts if is_leaderboard_eligible(a)]

    # Ranking metric: Svoya Igra ranks by final game points (values 10–50, penalties, passes);
    # every other mode keeps the existing Zakovat logic (number of correct answers).
    ranking_metric = "score" if version.game_mode == "svoyak" else "correct"

    def primary_value(a: SoloAttempt) -> int:
        return a.total_score if ranking_metric == "score" else a.total_correct

    def rank_key(a: SoloAttempt):
        # 1. higher primary metric, 2. lower active time, 3. earlier completion
        return (
            -primary_value(a),
            a.active_time_seconds,
            a.completed_at.timestamp() if a.completed_at else 0,
        )

    # Group by user_id to pick each user's best eligible attempt (legacy results may have several)
    user_best: Dict[int, SoloAttempt] = {}
    for att in attempts:
        uid = att.user_id
        if uid not in user_best or rank_key(att) < rank_key(user_best[uid]):
            user_best[uid] = att

    # Sort best attempts across all users
    sorted_attempts = sorted(user_best.values(), key=rank_key)

    # Compute total questions for this version
    manifest = version.published_manifest or {}
    manifest_rounds = manifest.get("rounds", [])
    if manifest_rounds:
        total_questions = sum(len(r.get("questions", [])) for r in manifest_rounds)
    else:
        total_questions = sum(len(r.round_questions) for r in version.rounds)

    leaderboard_rows = []
    for rank, att in enumerate(sorted_attempts, start=1):
        mins = att.active_time_seconds // 60
        secs = att.active_time_seconds % 60
        # Identity follows the player's privacy setting; no internal user id is ever published.
        leaderboard_rows.append({
            "rank": rank,
            **public_identity(att.user, viewer_id),
            "correct_count": att.total_correct,
            "total_score": att.total_score,
            "total_questions": total_questions,
            "active_time_seconds": att.active_time_seconds,
            "formatted_time": f"{mins:02d}:{secs:02d}",
            "completed_at": att.completed_at.isoformat() if att.completed_at else None,
        })

    return {
        "quiz_id": version.quiz_id,
        "quiz_title": version.quiz.title if version.quiz else None,
        "version_number": version.version_number,
        "version_id": version.id,
        "total_questions": total_questions,
        "total_participants": len(leaderboard_rows),
        "ranking_metric": ranking_metric,
        "leaderboard": leaderboard_rows,
    }


# =========================================================
# ARENA / DISCOVERY ENDPOINTS
# =========================================================

@router.get("", status_code=status.HTTP_200_OK, dependencies=[Depends(no_store_identity)])
def list_quizzes(
    q: Optional[str] = None,
    game_mode: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # Fetch all published quiz versions with their parent quiz eagerly loaded in one query
    published_versions = (
        db.query(QuizVersion)
        .join(Quiz, QuizVersion.quiz_id == Quiz.id)
        .options(joinedload(QuizVersion.quiz))
        .filter(QuizVersion.status == "published")
        .order_by(QuizVersion.quiz_id.asc(), QuizVersion.version_number.desc())
        .all()
    )
    latest_by_quiz = {}
    for v in published_versions:
        if v.quiz_id not in latest_by_quiz:
            latest_by_quiz[v.quiz_id] = v

    results = []
    for latest_published in latest_by_quiz.values():
        quiz = latest_published.quiz
        if not quiz:
            continue

        manifest = latest_published.published_manifest or {}
        # Unlisted quizzes are hidden from discovery (Arena list), accessible only by direct link
        if manifest.get("visibility") == "unlisted":
            continue

        item_title = manifest.get("title") or quiz.title
        item_desc = manifest.get("description") if "description" in manifest else quiz.description
        item_category = manifest.get("category") or "Umumiy"
        item_game_mode = manifest.get("game_mode") or latest_published.game_mode or "modern_multiround"

        # Search filter
        if q and q.strip():
            term = normalize_uzbek_text(q.strip())
            title_norm = normalize_uzbek_text(item_title)
            desc_norm = normalize_uzbek_text(item_desc)
            if not (term in title_norm or term in desc_norm):
                continue

        # Game mode filter
        if game_mode and game_mode.strip().lower() not in ("all", "barchasi", ""):
            if item_game_mode.lower() != game_mode.strip().lower():
                continue

        # Category filter
        if category and category.strip().lower() not in ("all", "barchasi", ""):
            if item_category.lower() != category.strip().lower():
                continue

        # Compute rounds summary and question counts
        manifest_rounds = manifest.get("rounds", [])
        if manifest_rounds:
            total_questions = sum(len(r.get("questions", [])) for r in manifest_rounds)
            total_rounds = len(manifest_rounds)
            rounds_summary = [
                {
                    "sequence": r.get("sequence"),
                    "round_type": r.get("round_type"),
                    "name": CANONICAL_ROUND_TYPES.get(r.get("round_type"), {}).get("name", r.get("round_type")),
                    "questions_count": len(r.get("questions", []))
                }
                for r in manifest_rounds
            ]
        else:
            total_questions = sum(len(r.round_questions) for r in latest_published.rounds)
            total_rounds = len(latest_published.rounds)
            rounds_summary = [
                {
                    "sequence": r.sequence,
                    "round_type": r.round_type,
                    "name": CANONICAL_ROUND_TYPES.get(r.round_type, {}).get("name", r.round_type),
                    "questions_count": len(r.round_questions)
                }
                for r in latest_published.rounds
            ]

        # Top player from existing version-scoped leaderboard
        lb_data = fetch_version_leaderboard(latest_published, db)
        top_player = lb_data["leaderboard"][0] if lb_data["leaderboard"] else None

        results.append({
            "quiz_id": quiz.id,
            "version_id": latest_published.id,
            "title": item_title,
            "description": item_desc,
            "game_mode": item_game_mode,
            "category": item_category,
            "version_number": latest_published.version_number,
            "visibility": manifest.get("visibility", "public"),
            "published_at": latest_published.published_at.isoformat() if latest_published.published_at else None,
            "total_rounds": total_rounds,
            "total_questions": total_questions,
            "estimated_duration_minutes": max(1, round(total_questions * 1.25)),
            "rounds_summary": rounds_summary,
            "top_player": top_player,
        })

    # Newest added first. Ordered by quiz id (the order packs were added), not by published_at:
    # a pack whose publication timestamp is older than its neighbours (e.g. Pack 02, first published
    # in September and transferred later) would otherwise appear out of sequence below Pack 01.
    results.sort(key=lambda x: x["quiz_id"], reverse=True)
    return results


@router.get("/categories", status_code=status.HTTP_200_OK)
def get_categories(
    game_mode: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Returns unique categories from published public quizzes with standard fallback categories."""
    published_versions = (
        db.query(QuizVersion)
        .filter(QuizVersion.status == "published")
        .order_by(QuizVersion.quiz_id.asc(), QuizVersion.version_number.desc())
        .all()
    )
    latest_by_quiz = {}
    for v in published_versions:
        if v.quiz_id not in latest_by_quiz:
            latest_by_quiz[v.quiz_id] = v

    mode_filter = game_mode.strip().lower() if game_mode and game_mode.strip().lower() not in ("all", "barchasi") else None
    
    categories = set()
    for v in latest_by_quiz.values():
        manifest = v.published_manifest or {}
        if manifest.get("visibility") == "unlisted":
            continue
        v_mode = (v.game_mode or "").lower()
        if mode_filter and v_mode != mode_filter:
            continue
        cat = manifest.get("category")
        if cat and cat.strip() and cat.strip().lower() not in ("klassik zakovat", "classic_zakovat", "all", "barchasi"):
            categories.add(cat.strip())

    # No padding with fixed categories: a filter pill must lead to at least one visible pack.
    if not categories:
        if mode_filter == "svoyak":
            categories = {"Milliy meros", "Tarix", "Geografiya", "Madaniyat", "Mantiq"}
        elif mode_filter == "classic_zakovat":
            categories = {"Mantiq", "Tarix", "Adabiyot", "Fan va Texnika", "San'at"}
        else:
            categories = {"Adabiyot", "Fan va Texnika", "Geografiya", "Madaniyat", "Mantiq", "Milliy meros", "San'at", "Tarix"}

    return {"categories": sorted(list(categories))}


@router.get("/canonical-rounds", status_code=status.HTTP_200_OK)
def get_canonical_rounds():
    """Returns the agreed canonical modern multi-round types and supported quiz modes."""
    return list(CANONICAL_ROUND_TYPES.values())


@router.get("/drafts", status_code=status.HTTP_200_OK)
def list_draft_quizzes(db: Session = Depends(get_db), current_owner: User = Depends(verify_owner_access)):
    """Returns list of draft quiz versions created by editors."""
    draft_versions = (
        db.query(QuizVersion)
        .filter(QuizVersion.status == "draft")
        .order_by(QuizVersion.id.desc())
        .all()
    )
    results = []
    for v in draft_versions:
        rounds_list = []
        total_questions = 0
        for r in v.rounds:
            rqs_count = db.query(RoundQuestion).filter(RoundQuestion.round_id == r.id).count()
            total_questions += rqs_count
            rounds_list.append({
                "round_id": r.id,
                "sequence": r.sequence,
                "round_type": r.round_type,
                "questions_count": rqs_count,
            })
        results.append({
            "quiz_id": v.quiz.id,
            "version_id": v.id,
            "version_number": v.version_number,
            "title": v.quiz.title,
            "description": v.quiz.description,
            "game_mode": v.game_mode,
            "status": v.status,
            "created_at": v.quiz.created_at.isoformat() if v.quiz.created_at else None,
            "total_questions": total_questions,
            "rounds": rounds_list,
        })
    return results


@router.post("/draft", status_code=status.HTTP_201_CREATED)
def create_quiz_draft(payload: CreateQuizDraftRequest, db: Session = Depends(get_db), current_owner: User = Depends(verify_owner_access)):
    """Creates a new Quiz Draft with Quiz and QuizVersion (status='draft', published_manifest=None),
    attaching questions via RoundQuestion links. Questions in the Question Bank remain untouched (round_id=None).
    """
    if not payload.title or not payload.title.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Quiz nomi bo'sh bo'lishi mumkin emas",
        )

    # Normalize rounds structure
    rounds_to_create = []
    if payload.rounds and len(payload.rounds) > 0:
        for r in payload.rounds:
            if r.round_type not in CANONICAL_ROUND_TYPES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Noma'lum raund turi: {r.round_type}",
                )
            q_ids = []
            if r.questions:
                q_ids = [q.question_id for q in r.questions]
            elif r.question_ids:
                q_ids = r.question_ids
            if not q_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Raund {r.sequence} da kamida bitta savol bo'lishi shart",
                )
            rounds_to_create.append({
                "sequence": r.sequence,
                "round_type": r.round_type,
                "config": r.config or {},
                "question_ids": q_ids,
            })
    else:
        # Single round payload
        if not payload.round_type:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Raund turi ko'rsatilishi shart",
            )
        if payload.round_type not in CANONICAL_ROUND_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Noma'lum raund turi: {payload.round_type}",
            )
        if not payload.question_ids or len(payload.question_ids) == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Kamida bitta savol tanlanishi shart",
            )
        rounds_to_create.append({
            "sequence": 1,
            "round_type": payload.round_type,
            "config": payload.round_config or {},
            "question_ids": payload.question_ids,
        })

    # Validate all referenced questions exist
    all_q_ids = []
    for r in rounds_to_create:
        all_q_ids.extend(r["question_ids"])
    unique_q_ids = set(all_q_ids)

    found_qs = db.query(Question).filter(Question.id.in_(unique_q_ids)).all()
    found_map = {q.id: q for q in found_qs}
    missing_ids = [qid for qid in unique_q_ids if qid not in found_map]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Quyidagi savol ID lari topilmadi: {missing_ids}",
        )

    # Create Quiz container
    quiz = Quiz(
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
    )
    db.add(quiz)
    db.flush()

    # Create Draft Version (published_manifest MUST remain None)
    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        game_mode=payload.game_mode or "modern_multiround",
        status="draft",
        published_manifest=None,
    )
    db.add(version)
    db.flush()

    total_q_count = 0
    # Create Rounds and RoundQuestion entries
    for r_data in rounds_to_create:
        round_obj = Round(
            quiz_version_id=version.id,
            sequence=r_data["sequence"],
            round_type=r_data["round_type"],
            config=r_data["config"],
        )
        db.add(round_obj)
        db.flush()

        for idx, q_id in enumerate(r_data["question_ids"], start=1):
            q_obj = found_map[q_id]
            rq = RoundQuestion(
                round_id=round_obj.id,
                question_id=q_obj.id,
                sequence=idx,
                points_override=None,
                config_override=None,
            )
            db.add(rq)
            total_q_count += 1

    db.commit()
    db.refresh(quiz)
    db.refresh(version)

    return {
        "success": True,
        "quiz_id": quiz.id,
        "version_id": version.id,
        "version_number": version.version_number,
        "status": version.status,
        "title": quiz.title,
        "rounds_count": len(rounds_to_create),
        "questions_count": total_q_count,
    }


@router.post("/{quiz_id}/publish/{version_number}", status_code=status.HTTP_200_OK)
def publish_quiz_version(quiz_id: int, version_number: int, db: Session = Depends(get_db), current_owner: User = Depends(verify_owner_access)):
    """
    Publish quiz version endpoint.
    Delegates directly to authoritative execute_publish_version service,
    enforcing comprehensive publish-readiness validation and idempotency.
    """
    from api.drafts import execute_publish_version

    version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.version_number == version_number)
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    return execute_publish_version(version, db)


def public_question_stub(sequence: Any, points: Any) -> dict:
    """The only per-question data the public quiz detail may expose before gameplay."""
    return {"sequence": sequence, "points": points if points is not None else 1}


@router.get("/{quiz_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(no_store_identity)])
def get_quiz_detail(quiz_id: int, db: Session = Depends(get_db),
                    viewer: Optional[User] = Depends(get_current_user_optional)):
    """Returns the ordered structure of a quiz for display and play, with leaderboard preview."""
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

    latest_version = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not latest_version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No published version found for this quiz")

    manifest = latest_version.published_manifest or {}

    # If published_manifest is present, read directly from immutable snapshot
    if manifest and "rounds" in manifest:
        manifest_rounds = manifest["rounds"]
        rounds_data = []
        for mr in manifest_rounds:
            # PUBLIC catalog response: structure only. Never expose question stems, options, media,
            # answers or explanations before gameplay (counts/points are enough for the detail view).
            clean_questions = [public_question_stub(mq.get("sequence"), mq.get("points", 1))
                               for mq in mr.get("questions", [])]
            rounds_data.append({
                "id": mr.get("round_id"),
                "sequence": mr.get("sequence"),
                "round_type": mr.get("round_type"),
                "name": CANONICAL_ROUND_TYPES.get(mr.get("round_type"), {}).get("name", mr.get("round_type")),
                "config": sanitize_round_config(mr.get("config", {})),
                "questions": clean_questions,
            })
    else:
        # Live relational fallback (for legacy versions before manifest was introduced)
        rounds = (
            db.query(Round)
            .filter(Round.quiz_version_id == latest_version.id)
            .order_by(Round.sequence.asc())
            .all()
        )
        rounds_data = []
        for r in rounds:
            rqs = (
                db.query(RoundQuestion)
                .filter(RoundQuestion.round_id == r.id)
                .order_by(RoundQuestion.sequence.asc())
                .all()
            )
            if rqs:
                q_list = []
                for rq in rqs:
                    q = rq.question
                    pts = (
                        rq.points_override
                        if rq.points_override is not None
                        else (q.default_points if q.default_points is not None else q.points)
                    )
                    # PUBLIC catalog response: structure only (no stems/options/media/answers)
                    q_list.append(public_question_stub(rq.sequence, pts if pts is not None else 1))
            else:
                sorted_questions = sorted(r.questions, key=lambda q: q.sequence or 0)
                q_list = [public_question_stub(q.sequence, q.points) for q in sorted_questions]

            rounds_data.append({
                "id": r.id,
                "sequence": r.sequence,
                "round_type": r.round_type,
                "name": CANONICAL_ROUND_TYPES.get(r.round_type, {}).get("name", r.round_type),
                "config": sanitize_round_config(r.config or {}),
                "questions": q_list,
            })

    # Fetch version-scoped leaderboard for preview & #1 player
    lb_data = fetch_version_leaderboard(latest_version, db, viewer.id if viewer else None)
    top_player = lb_data["leaderboard"][0] if lb_data["leaderboard"] else None
    leaderboard_preview = lb_data["leaderboard"][:5]

    total_questions = sum(len(r.get("questions", [])) for r in rounds_data)
    est_duration = max(1, round(total_questions * 1.25))

    rounds_summary = [
        {
            "sequence": r.get("sequence"),
            "round_type": r.get("round_type"),
            "name": r.get("name"),
            "questions_count": len(r.get("questions", [])),
        }
        for r in rounds_data
    ]

    return {
        "quiz_id": quiz.id,
        "version_id": latest_version.id,
        "title": manifest.get("title") or quiz.title,
        "description": manifest.get("description") if "description" in manifest else quiz.description,
        "game_mode": manifest.get("game_mode") or latest_version.game_mode,
        "category": manifest.get("category") or "Umumiy",
        "visibility": manifest.get("visibility", "public"),
        "version_number": latest_version.version_number,
        "published_at": latest_version.published_at.isoformat() if latest_version.published_at else None,
        "total_rounds": len(rounds_data),
        "total_questions": total_questions,
        "estimated_duration_minutes": est_duration,
        "top_player": top_player,
        "leaderboard_preview": leaderboard_preview,
        "rounds": rounds_data,
        "rounds_summary": rounds_summary,
    }


# =========================================================
# LEADERBOARD ENDPOINTS
# =========================================================


@router.get("/{quiz_id}/leaderboard", status_code=status.HTTP_200_OK, dependencies=[Depends(no_store_identity)])
def get_quiz_leaderboard(quiz_id: int, db: Session = Depends(get_db),
                         viewer: Optional[User] = Depends(get_current_user_optional)):
    """Leaderboard for the latest published version of a quiz."""
    latest_published = (
        db.query(QuizVersion)
        .filter(QuizVersion.quiz_id == quiz_id, QuizVersion.status == "published")
        .order_by(QuizVersion.version_number.desc())
        .first()
    )
    if not latest_published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nashr qilingan versiya topilmadi")

    return fetch_version_leaderboard(latest_published, db, viewer.id if viewer else None)


@router.get("/{quiz_id}/versions/{version_number}/leaderboard", status_code=status.HTTP_200_OK, dependencies=[Depends(no_store_identity)])
def get_version_leaderboard(quiz_id: int, version_number: int, db: Session = Depends(get_db),
                            viewer: Optional[User] = Depends(get_current_user_optional)):
    """Leaderboard for a specific published version of a quiz."""
    version = (
        db.query(QuizVersion)
        .filter(
            QuizVersion.quiz_id == quiz_id,
            QuizVersion.version_number == version_number,
            QuizVersion.status == "published",
        )
        .first()
    )
    if not version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nashr qilingan versiya topilmadi")

    return fetch_version_leaderboard(version, db, viewer.id if viewer else None)