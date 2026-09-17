from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_
from database import get_db
from models import Question, AcceptedAnswer

router = APIRouter(prefix="/api/questions", tags=["questions"])


@router.get("", status_code=status.HTTP_200_OK)
def list_questions(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(None, description="Search text in questions"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (ready, needs_review, draft)"),
    category: Optional[str] = Query(None, description="Filter by category"),
    round_type: Optional[str] = Query(None, description="Filter by round_type"),
    source: Optional[str] = Query(None, description="Filter by provenance source"),
    db: Session = Depends(get_db),
):
    """
    Paginated Question Bank explorer.
    Never loads all records at once; uses database-level pagination.
    """
    query = db.query(Question)

    # 1. Status filter
    if status_filter and status_filter.lower() != "all":
        query = query.filter(Question.status == status_filter.lower())

    # 2. Category filter
    if category and category.lower() != "all":
        query = query.filter(Question.category == category)

    # 3. Round type filter (checks compound_type or source_meta->round_type)
    if round_type and round_type.lower() != "all":
        # SQLite vs PostgreSQL astext compatibility
        try:
            query = query.filter(
                or_(
                    Question.compound_type == round_type,
                    Question.source_meta["round_type"].astext == round_type,
                )
            )
        except Exception:
            pass

    # 4. Source / Provenance filter
    if source and source.lower() != "all":
        try:
            query = query.filter(
                or_(
                    Question.source_meta["source_name"].astext == source,
                    Question.source_meta["channel_name"].astext == source,
                    Question.source_meta["source_file"].astext == source,
                )
            )
        except Exception:
            pass

    # 5. Search text
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(Question.text.ilike(term))

    total = query.count()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # Load items with primary accepted answer eagerly loaded
    items_raw = (
        query.options(joinedload(Question.accepted_answers))
        .order_by(Question.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = []
    for q in items_raw:
        primary_ans = next(
            (a.answer_text for a in q.accepted_answers if a.is_primary),
            (q.source_meta or {}).get("primary_answer") if q.source_meta else None
        )
        sm = q.source_meta or {}
        source_label = sm.get("source_name") or sm.get("channel_name") or sm.get("source_file") or "Noma'lum"
        preview = q.text[:120] + "..." if len(q.text) > 120 else q.text

        items.append({
            "id": q.id,
            "text": q.text,
            "text_preview": preview,
            "status": q.status,
            "category": q.category or "Umumiy",
            "round_type": q.compound_type or sm.get("round_type") or "standard",
            "points": q.points or q.default_points or 1,
            "default_points": q.default_points or 1,
            "question_type": q.question_type or "text",
            "source": source_label,
            "has_media": bool(q.media_url),
            "primary_answer": primary_ans,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/filters", status_code=status.HTTP_200_OK)
def get_filter_options(db: Session = Depends(get_db)):
    """
    Returns available distinct statuses, categories, round types, and sources
    to dynamically populate filter dropdowns in the Question Bank UI.
    """
    # Distinct categories
    categories = [
        c[0] for c in db.query(Question.category)
        .filter(Question.category.isnot(None))
        .distinct()
        .all()
        if c[0]
    ]

    # Distinct statuses
    statuses = [
        s[0] for s in db.query(Question.status)
        .distinct()
        .all()
        if s[0]
    ]

    # Extract distinct sources from source_meta
    sources_set = set()
    sample_sm = db.query(Question.source_meta).filter(Question.source_meta.isnot(None)).limit(1500).all()
    for row in sample_sm:
        sm = row[0] or {}
        src = sm.get("source_name") or sm.get("channel_name") or sm.get("source_file")
        if src:
            sources_set.add(str(src))

    round_types = ["standard", "blitz", "duplet", "jeopardy"]

    return {
        "statuses": sorted(statuses),
        "categories": sorted(categories),
        "sources": sorted(list(sources_set)),
        "round_types": round_types,
    }


@router.get("/{question_id}", status_code=status.HTTP_200_OK)
def get_question_detail(question_id: int, db: Session = Depends(get_db)):
    """
    Full read-only detail of a Question Bank item, including all accepted answers,
    media, explanation, and provenance.
    """
    q = (
        db.query(Question)
        .options(joinedload(Question.accepted_answers))
        .filter(Question.id == question_id)
        .first()
    )
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Savol topilmadi")

    sm = q.source_meta or {}
    primary_ans = next(
        (a.answer_text for a in q.accepted_answers if a.is_primary),
        sm.get("primary_answer")
    )

    accepted = [
        {
            "id": a.id,
            "answer_text": a.answer_text,
            "is_primary": a.is_primary,
        }
        for a in q.accepted_answers
    ]

    return {
        "id": q.id,
        "text": q.text,
        "status": q.status,
        "category": q.category,
        "round_type": q.compound_type or sm.get("round_type") or "standard",
        "points": q.points or 1,
        "default_points": q.default_points or 1,
        "explanation": q.explanation,
        "question_type": q.question_type or "text",
        "options": q.options,
        "media_provider": q.media_provider,
        "media_url": q.media_url,
        "compound_group_id": q.compound_group_id,
        "compound_type": q.compound_type,
        "source_meta": sm,
        "primary_answer": primary_ans,
        "accepted_answers": accepted,
    }
