"""Signed-in player's own progress (read-only): pack progress for the Arena and the game to resume."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.auth import get_current_user_optional, get_current_user_required
from database import get_db
from models import User
from services.player_progress import pack_progress, resumable_game

router = APIRouter(prefix="/api/me", tags=["player_progress"])


@router.get("/pack-progress")
def get_pack_progress(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """{quiz_id: progress} for packs this player has touched. Only ever the caller's own attempts."""
    progress = pack_progress(db, current_user.id)
    return {"packs": {str(quiz_id): data for quiz_id, data in progress.items()}}


@router.get("/active-game")
def get_active_game(
    quiz_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """The caller's most recent resumable game (optionally for one pack). Never creates or changes a game.
    Anonymous visitors simply get no game (the homepage asks on every visit; a 401 would only add noise)."""
    if current_user is None:
        return {"game": None}
    return {"game": resumable_game(db, current_user.id, quiz_id=quiz_id)}
