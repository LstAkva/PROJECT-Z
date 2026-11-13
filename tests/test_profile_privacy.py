"""
Public-profile privacy. One server-side rule (api.auth.profile_is_public / public_identity) decides what
other people may see, for the public profile endpoints AND for every leaderboard.

* Public profile: display name, @username, avatar, bio, join date, stats (if show_stats) — never the email
  or the internal id.
* Private profile: other people get exactly the same 404 as for an account that does not exist, by
  username or by numeric id, so private accounts can be neither read nor enumerated. On leaderboards the
  result stays (rank, score, time) but the player is shown as an anonymous placeholder.
* The owner always sees their own profile and their own real row.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status

from api.auth import ANONYMOUS_PLAYER_NAME, create_access_token, hash_password
from models import QuizVersion, SoloAttempt, User
from tests.test_player_progress import make_classic_pack

PUBLIC_PATHS = ("/api/users/{ref}/public", "/api/auth/users/{ref}/public")


def _user(db, email, name, username=None, public=True, avatar=None, bio=None, show_stats=True):
    user = User(email=email, display_name=name, username=username, is_public=public, avatar_url=avatar, bio=bio,
                show_stats=show_stats, hashed_password=hash_password("Pass123!"), auth_provider="local")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hdr(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


@pytest.fixture
def people(db_session):
    return {
        "public": _user(db_session, "pub@zakowhat.uz", "Ochiq O‘yinchi", "ochiq_oyinchi", True,
                        "/static/uploads/avatars/pub.png", "Kitobxon"),
        "public_no_handle": _user(db_session, "nohandle@zakowhat.uz", "Taxallussiz", None, True),
        "private": _user(db_session, "secret@zakowhat.uz", "Maxfiy Ism", "maxfiy_handle", False,
                         "/static/uploads/avatars/secret.png", "Shaxsiy bio"),
        "viewer": _user(db_session, "viewer@zakowhat.uz", "Kuzatuvchi", "kuzatuvchi", True),
    }


@pytest.fixture
def anon(client):
    client.cookies.clear()          # the conftest client starts signed in; these tests choose their identity
    return client


def _get(client, path, user=None):
    return client.get(path, headers=_hdr(user) if user else {})


SECRETS = ("Maxfiy Ism", "maxfiy_handle", "secret.png", "Shaxsiy bio", "secret@zakowhat.uz", "created_at")


# =========================================================================== public profile endpoint

@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_profile_shows_only_public_fields(anon, people, path):
    body = _get(anon, path.format(ref="ochiq_oyinchi")).json()
    assert body["display_name"] == "Ochiq O‘yinchi" and body["username"] == "ochiq_oyinchi"
    assert body["avatar_url"].endswith("pub.png") and body["bio"] == "Kitobxon"
    assert body["is_private"] is False and body["is_self"] is False and body["created_at"]
    assert "email" not in body and "id" not in body and "hashed_password" not in body


@pytest.mark.parametrize("path", PUBLIC_PATHS)
@pytest.mark.parametrize("as_who", ["anonymous", "another_user"])
def test_private_profile_reveals_nothing_to_others(anon, people, path, as_who):
    viewer = people["viewer"] if as_who == "another_user" else None
    for ref in ("maxfiy_handle", str(people["private"].id)):
        res = _get(anon, path.format(ref=ref), viewer)
        assert res.status_code == status.HTTP_404_NOT_FOUND
        for secret in SECRETS:                                  # name, avatar, bio, join date, email
            assert secret not in res.text, (ref, secret)


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_private_accounts_cannot_be_told_apart_from_missing_ones(anon, people, path):
    """Enumeration: walking sequential ids gives identical answers for private and non-existent accounts."""
    private = _get(anon, path.format(ref=str(people["private"].id)))
    missing = _get(anon, path.format(ref="999999"))
    missing_name = _get(anon, path.format(ref="hech_kim_yoq"))
    assert private.status_code == missing.status_code == missing_name.status_code == status.HTTP_404_NOT_FOUND
    assert private.json() == missing.json() == missing_name.json()
    found = []
    for user_id in range(1, max(u.id for u in people.values()) + 5):
        res = _get(anon, path.format(ref=str(user_id)))
        if res.status_code == status.HTTP_200_OK:
            found.append(res.json()["display_name"])
    assert "Maxfiy Ism" not in found
    assert {"Ochiq O‘yinchi", "Taxallussiz", "Kuzatuvchi"} <= set(found)   # public profiles keep working


def test_owner_always_sees_their_own_private_profile(anon, people):
    owner = people["private"]
    for ref in ("maxfiy_handle", str(owner.id)):
        body = _get(anon, f"/api/users/{ref}/public", owner).json()
        assert body["display_name"] == "Maxfiy Ism" and body["is_self"] is True and body["is_private"] is True
        assert "email" not in body and "id" not in body


def test_visibility_setting_is_respected_both_ways(anon, people, db_session):
    user = people["public"]
    assert _get(anon, "/api/users/ochiq_oyinchi/public").status_code == status.HTTP_200_OK
    # the owner switches to private through the normal settings endpoint
    assert anon.patch("/api/auth/profile", json={"is_public": False}, headers=_hdr(user)).status_code == 200
    assert _get(anon, "/api/users/ochiq_oyinchi/public").status_code == status.HTTP_404_NOT_FOUND
    assert anon.patch("/api/auth/profile", json={"is_public": True}, headers=_hdr(user)).status_code == 200
    assert _get(anon, "/api/users/ochiq_oyinchi/public").status_code == status.HTTP_200_OK


def test_show_stats_still_hides_statistics_on_a_public_profile(anon, db_session):
    _user(db_session, "nostats@zakowhat.uz", "Statistikasiz", "statistikasiz", True, show_stats=False)
    body = _get(anon, "/api/users/statistikasiz/public").json()
    assert body["display_name"] == "Statistikasiz" and body["stats"] is None and body["show_stats"] is False


# =========================================================================== leaderboards

@pytest.fixture
def board(db_session, people):
    """A Classic pack with one finished (legacy-ranked) result per player, best first: private, public, no-handle."""
    quiz = make_classic_pack(db_session, "Maxfiylik To‘plami")
    version = db_session.query(QuizVersion).filter_by(quiz_id=quiz.id).one()
    now = datetime.now(timezone.utc)
    for n, (user, correct) in enumerate(((people["private"], 6), (people["public"], 5), (people["public_no_handle"], 4))):
        db_session.add(SoloAttempt(quiz_version_id=version.id, user_id=user.id, timer_mode="standard", status="completed",
                                   total_correct=correct, total_score=correct, active_time_seconds=60 + n,
                                   completed_at=now - timedelta(minutes=10 - n)))
    db_session.commit()
    return quiz


def _rows(client, quiz, user=None):
    res = _get(client, f"/api/quizzes/{quiz.id}/leaderboard", user)
    assert res.status_code == status.HTTP_200_OK
    return res.json()["leaderboard"]


def test_private_player_keeps_their_rank_but_is_anonymous(anon, board, people):
    rows = _rows(anon, board)
    assert [r["rank"] for r in rows] == [1, 2, 3] and [r["correct_count"] for r in rows] == [6, 5, 4]
    private_row = rows[0]
    assert private_row["display_name"] == ANONYMOUS_PLAYER_NAME
    assert private_row["username"] is None and private_row["avatar_url"] is None and private_row["profile_ref"] is None
    assert private_row["is_private"] is True and private_row["is_me"] is False
    assert private_row["formatted_time"] and private_row["completed_at"]           # the result itself stays


def test_public_players_appear_normally_without_internal_ids(anon, board, people):
    rows = _rows(anon, board)
    assert rows[1]["display_name"] == "Ochiq O‘yinchi" and rows[1]["profile_ref"] == "ochiq_oyinchi"
    assert rows[1]["avatar_url"].endswith("pub.png")
    assert rows[2]["display_name"] == "Taxallussiz" and rows[2]["profile_ref"] is None     # no username: no link
    for row in rows:
        assert "user_id" not in row and "email" not in row and "id" not in row


def test_no_leaderboard_surface_leaks_a_private_identity(anon, board, people):
    detail = _get(anon, f"/api/quizzes/{board.id}").json()
    listed = next(q for q in _get(anon, "/api/quizzes").json() if q["quiz_id"] == board.id)
    version = _get(anon, f"/api/quizzes/{board.id}/versions/1/leaderboard").json()
    for payload in (detail["top_player"], detail["leaderboard_preview"], listed.get("top_player"), version):
        text = str(payload)
        for secret in SECRETS[:4] + ("secret@zakowhat.uz",):
            assert secret not in text, secret
        assert "user_id" not in text


def test_a_private_player_sees_their_own_row(anon, board, people):
    rows = _rows(anon, board, people["private"])
    assert rows[0]["is_me"] is True and rows[0]["display_name"] == "Maxfiy Ism"
    assert sum(r["is_me"] for r in rows) == 1
    # ...while everyone else, signed in or not, still sees the placeholder
    assert _rows(anon, board, people["viewer"])[0]["display_name"] == ANONYMOUS_PLAYER_NAME
    detail = _get(anon, f"/api/quizzes/{board.id}", people["private"]).json()
    assert detail["top_player"]["is_me"] is True


def test_identity_responses_are_never_cached(anon, board, people):
    """Payloads differ per viewer and per visibility setting, so no cache may store them: otherwise an
    owner's view of their own private profile, or a row from before a switch to private, could be replayed."""
    paths = ["/api/users/ochiq_oyinchi/public", "/api/auth/users/ochiq_oyinchi/public", "/api/quizzes",
             f"/api/quizzes/{board.id}", f"/api/quizzes/{board.id}/leaderboard",
             f"/api/quizzes/{board.id}/versions/1/leaderboard"]
    for user in (None, people["private"]):
        for path in paths:
            res = _get(anon, path, user)
            assert res.status_code == status.HTTP_200_OK, path
            assert res.headers.get("cache-control") == "private, no-store", path
    own_private = _get(anon, "/api/users/maxfiy_handle/public", people["private"])
    assert own_private.status_code == status.HTTP_200_OK
    assert own_private.headers.get("cache-control") == "private, no-store"


def test_leaderboard_profile_links_open_public_profiles(anon, board, people):
    for row in _rows(anon, board):
        if row["profile_ref"]:
            assert _get(anon, f"/api/users/{row['profile_ref']}/public").status_code == status.HTTP_200_OK
