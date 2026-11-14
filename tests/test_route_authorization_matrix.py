import os
import uuid
import pytest
from fastapi import status
from fastapi.routing import APIRoute

from main import app
from database import get_db
from models import User
from services.gameplay import is_conservative_typo_match


# ==============================================================================
# ROUTE CLASSIFICATION REGISTRY (107 TOTAL ENDPOINTS)
# ==============================================================================

OWNER_ONLY_ROUTES = {
    # Owner Control Mode (demonstration sessions) — verify_owner_access on every route
    ("POST", "/api/demo/start/{quiz_id}"),
    ("GET", "/api/demo/{session_token}"),
    ("POST", "/api/demo/{session_token}/jump"),
    ("POST", "/api/demo/{session_token}/skip"),
    ("POST", "/api/demo/{session_token}/previous"),
    ("POST", "/api/demo/{session_token}/reveal"),
    ("POST", "/api/demo/{session_token}/timer"),
    ("POST", "/api/demo/{session_token}/score"),
    ("POST", "/api/demo/{session_token}/end"),
    ("POST", "/api/demo/{session_token}/restart"),
    # Drafts Authoring API
    ("POST", "/api/drafts"),
    ("GET", "/api/drafts"),
    ("GET", "/api/drafts/{draft_id}"),
    ("PUT", "/api/drafts/{draft_id}"),
    ("DELETE", "/api/drafts/{draft_id}"),
    ("POST", "/api/drafts/{draft_id}/rounds"),
    ("PUT", "/api/drafts/{draft_id}/rounds/reorder"),
    ("PUT", "/api/drafts/{draft_id}/rounds/{round_id}"),
    ("DELETE", "/api/drafts/{draft_id}/rounds/{round_id}"),
    ("POST", "/api/drafts/{draft_id}/rounds/{round_id}/questions"),
    ("PUT", "/api/drafts/{draft_id}/rounds/{round_id}/questions/reorder"),
    ("PUT", "/api/drafts/{draft_id}/rounds/{round_id}/questions/{rq_id}"),
    ("DELETE", "/api/drafts/{draft_id}/rounds/{round_id}/questions/{rq_id}"),
    ("POST", "/api/drafts/{draft_id}/validate"),
    ("GET", "/api/drafts/{draft_id}/preview"),
    ("POST", "/api/drafts/{draft_id}/preview/start"),
    ("POST", "/api/drafts/{draft_id}/publish"),
    # Quizzes Draft & Publishing
    ("GET", "/api/quizzes/drafts"),
    ("POST", "/api/quizzes/draft"),
    ("POST", "/api/quizzes/{quiz_id}/publish/{version_number}"),
    # Question Bank API
    ("GET", "/api/questions"),
    ("GET", "/api/questions/filters"),
    ("GET", "/api/questions/{question_id}"),
    # Owner Panel API
    ("GET", "/api/owner/status"),
    ("GET", "/api/owner/daily-bank"),  # read-only Daily bank monitoring
    ("GET", "/api/owner/quizzes"),
    ("POST", "/api/owner/quizzes"),
    ("GET", "/api/owner/quizzes/{quiz_id}"),
    ("PUT", "/api/owner/quizzes/{quiz_id}"),
    ("DELETE", "/api/owner/quizzes/{quiz_id}"),
    ("POST", "/api/owner/quizzes/{quiz_id}/archive"),
    ("POST", "/api/owner/quizzes/{quiz_id}/decision"),
    ("POST", "/api/owner/quizzes/{quiz_id}/rounds"),
    ("POST", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/questions"),
    ("POST", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/attach-question"),
    ("DELETE", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/questions/{rq_id}"),
    ("PUT", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/questions/{rq_id}/move"),
    ("PUT", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/questions/reorder"),
    ("PUT", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/questions/{rq_id}/replace"),
    ("POST", "/api/owner/quizzes/{quiz_id}/rounds/{round_id}/questions/{rq_id}/replace"),
    ("POST", "/api/owner/quizzes/{quiz_id}/validate"),
    ("POST", "/api/owner/quizzes/{quiz_id}/publish"),
    ("POST", "/api/owner/quizzes/{quiz_id}/unpublish"),
    ("GET", "/api/owner/questions"),
    ("GET", "/api/owner/questions/{question_id}"),
    ("POST", "/api/owner/questions/{question_id}/decision"),
    ("POST", "/api/owner/questions/batch-decision"),
    ("POST", "/api/owner/questions/{question_id}/answers"),
    ("DELETE", "/api/owner/questions/{question_id}/answers/{answer_id}"),
    ("PUT", "/api/owner/questions/{question_id}/answers/{answer_id}/primary"),
    ("POST", "/api/owner/pack-generator/preview"),
    ("POST", "/api/owner/pack-generator/generate"),
    ("GET", "/api/owner/users"),
    ("GET", "/api/owner/users/{user_id}"),
    ("PUT", "/api/owner/users/{user_id}"),
    # Protected UI HTML Views (redirect non-owner to /owner)
    ("GET", "/bank"),
    ("GET", "/admin"),
    ("GET", "/builder"),
    ("GET", "/builder/{draft_id}"),
    ("GET", "/create"),
    ("GET", "/owner"),
    ("GET", "/owner/{subpath:path}"),
}

STAGING_AUTHORIZED_ROUTES = {
    ("GET", "/api/play/staging/quizzes"),
    ("GET", "/api/play/staging/quizzes/{quiz_id}"),
    ("POST", "/api/play/staging/start/{quiz_id}"),
}

AUTHENTICATED_PLAYER_ROUTES = {
    ("GET", "/api/auth/profile"),
    ("PATCH", "/api/auth/profile"),
    ("POST", "/api/auth/avatar"),
    ("DELETE", "/api/auth/avatar"),
    ("POST", "/api/auth/change-password"),
    ("POST", "/api/auth/consent"),
    ("DELETE", "/api/auth/account"),
    ("GET", "/api/daily/history"),
    ("GET", "/api/daily/stats"),  # the signed-in player's own streak
    ("GET", "/api/me/pack-progress"),  # own Arena progress
    ("POST", "/api/play/start/{quiz_id}"),
    ("GET", "/api/play/{session_token}"),
    ("GET", "/api/play/{session_token}/board"),
    ("POST", "/api/play/{session_token}/select"),
    ("POST", "/api/play/{session_token}/answer"),
    ("GET", "/api/play/{session_token}/reveal"),
    ("POST", "/api/play/{session_token}/continue"),
    ("POST", "/api/play/{session_token}/restart"),
    ("GET", "/api/play/{session_token}/results"),
    ("GET", "/api/play/{session_token}/review"),
}

PUBLIC_ROUTES = {
    # Public Root & Legal Views
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/robots.txt"),  # crawler hints, no data
    ("GET", "/sitemap.xml"),  # public page list, no data
    ("GET", "/api/me/active-game"),  # anonymous: {"game": null}; signed in: only the caller's own game
    ("GET", "/daily"),
    ("GET", "/privacy"),
    ("GET", "/terms"),
    ("GET", "/arena"),
    ("GET", "/profile"),
    ("GET", "/profile/{user_id_or_username}"),
    ("GET", "/u/{username}"),
    ("GET", "/quiz/{quiz_id}"),
    ("GET", "/play/{quiz_id}"),
    ("GET", "/staging"),
    ("GET", "/staging/{quiz_id}"),
    ("GET", "/preview/grand-sanctum"),
    ("GET", "/preview/homepage"),
    # Public Quizzes Catalog
    ("GET", "/api/quizzes"),
    ("GET", "/api/quizzes/categories"),
    ("GET", "/api/quizzes/canonical-rounds"),
    ("GET", "/api/quizzes/{quiz_id}"),
    ("GET", "/api/quizzes/{quiz_id}/leaderboard"),
    ("GET", "/api/quizzes/{quiz_id}/versions/{version_number}/leaderboard"),
    # Public Auth
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/config"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/google"),
    ("GET", "/api/users/{identifier}/public"),
    ("GET", "/api/auth/users/{identifier}/public"),
    # Public Daily Question
    ("GET", "/api/daily/today"),
    ("POST", "/api/daily/submit"),
}


# ==============================================================================
# MATRIX COVERAGE & ENFORCEMENT TESTS
# ==============================================================================

def test_every_api_route_is_classified_in_authorization_matrix():
    """
    Enforces that NO route in the FastAPI application can exist without explicit
    classification in the security authorization matrix. Fails closed on newly added routes.
    """
    all_classified = OWNER_ONLY_ROUTES | STAGING_AUTHORIZED_ROUTES | AUTHENTICATED_PLAYER_ROUTES | PUBLIC_ROUTES

    collected = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for m in route.methods:
                if m not in ("OPTIONS", "HEAD"):
                    collected.add((m, route.path))
        elif type(route).__name__ == "_IncludedRouter":
            for sub in route.original_router.routes:
                if isinstance(sub, APIRoute):
                    for m in sub.methods:
                        if m not in ("OPTIONS", "HEAD"):
                            collected.add((m, sub.path))

    unclassified = collected - all_classified
    assert not unclassified, (
        f"CRITICAL SECURITY DEFECT: The following routes lack explicit classification in the "
        f"authorization matrix: {unclassified}. Every route must be classified."
    )


def test_anonymous_access_denied_on_all_sensitive_routes(client):
    """
    Verifies that an anonymous visitor receives HTTP 401 Unauthorized
    (or HTTP 303 Redirect to /owner for HTML views) on all owner and draft routes.
    Drafts, questions, and unpublished content are NEVER exposed to the public.
    """
    client.cookies.clear()

    # 1. Drafts API
    assert client.get("/api/drafts").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/drafts", json={"title": "Hack"}).status_code == status.HTTP_401_UNAUTHORIZED
    assert client.get("/api/drafts/1").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.put("/api/drafts/1", json={"title": "Hack"}).status_code == status.HTTP_401_UNAUTHORIZED
    assert client.delete("/api/drafts/1").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/drafts/1/rounds", json={"round_type": "standard"}).status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/drafts/1/validate").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.get("/api/drafts/1/preview").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/drafts/1/publish").status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Quizzes draft/publish
    assert client.get("/api/quizzes/drafts").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/quizzes/draft", json={"title": "Hack"}).status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/quizzes/1/publish/1").status_code == status.HTTP_401_UNAUTHORIZED

    # 3. Question Bank
    assert client.get("/api/questions").status_code == status.HTTP_401_UNAUTHORIZED

    # 4. Owner Panel
    assert client.get("/api/owner/status").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.get("/api/owner/quizzes").status_code == status.HTTP_401_UNAUTHORIZED

    # 5. Staging routes
    assert client.get("/api/play/staging/quizzes").status_code == status.HTTP_401_UNAUTHORIZED

    # 6. HTML Admin views redirect unauthenticated users
    res_builder = client.get("/builder", follow_redirects=False)
    assert res_builder.status_code == status.HTTP_303_SEE_OTHER
    assert res_builder.headers["location"] == "/owner"

    res_bank = client.get("/bank", follow_redirects=False)
    assert res_bank.status_code == status.HTTP_303_SEE_OTHER


def test_authenticated_non_owner_denied_on_owner_routes(client, monkeypatch):
    """
    Verifies that an authenticated regular player receives HTTP 403 Forbidden
    when attempting to read or mutate drafts, publish quizzes, or access the Question Bank.
    """
    owner_email = "the_real_owner@zakowhat.uz"
    monkeypatch.setenv("OWNER_EMAIL", owner_email)

    # Register & login as a non-owner regular player
    player_email = f"regular_player_{uuid.uuid4().hex[:6]}@zakowhat.uz"
    client.cookies.clear()
    reg = client.post("/api/auth/register", json={
        "display_name": "Normal Player",
        "email": player_email,
        "password": "Password123!",
    })
    assert reg.status_code == status.HTTP_201_CREATED

    # 1. Drafts API -> 403 Forbidden
    assert client.get("/api/drafts").status_code == status.HTTP_403_FORBIDDEN
    assert client.post("/api/drafts", json={"title": "Exploit"}).status_code == status.HTTP_403_FORBIDDEN
    assert client.get("/api/drafts/1").status_code == status.HTTP_403_FORBIDDEN
    assert client.delete("/api/drafts/1").status_code == status.HTTP_403_FORBIDDEN
    assert client.post("/api/drafts/1/publish").status_code == status.HTTP_403_FORBIDDEN

    # 2. Quizzes draft/publish -> 403 Forbidden
    assert client.get("/api/quizzes/drafts").status_code == status.HTTP_403_FORBIDDEN
    assert client.post("/api/quizzes/draft", json={"title": "Exploit"}).status_code == status.HTTP_403_FORBIDDEN
    assert client.post("/api/quizzes/1/publish/1").status_code == status.HTTP_403_FORBIDDEN

    # 3. Question Bank -> 403 Forbidden
    assert client.get("/api/questions").status_code == status.HTTP_403_FORBIDDEN

    # 4. Owner Panel -> 403 Forbidden
    assert client.get("/api/owner/status").status_code == status.HTTP_403_FORBIDDEN


def test_authorized_owner_workflow(client, monkeypatch):
    """
    Verifies that the configured owner can access and operate the draft authoring,
    question bank, and publishing workflows successfully.
    """
    owner_email = f"owner_{uuid.uuid4().hex[:6]}@zakowhat.uz"
    monkeypatch.setenv("OWNER_EMAIL", owner_email)

    client.cookies.clear()
    reg = client.post("/api/auth/register", json={
        "display_name": "Authorized Owner",
        "email": owner_email,
        "password": "ValidOwnerPassword123!",
    })
    assert reg.status_code == status.HTTP_201_CREATED

    # 1. Create a draft via /api/drafts
    res_draft = client.post("/api/drafts", json={
        "title": "Championship 2026 Authoring",
        "description": "Owner authored pack",
        "game_mode": "modern_multiround",
    })
    assert res_draft.status_code == status.HTTP_201_CREATED
    draft_id = res_draft.json()["version_id"]

    # 2. Add round
    res_round = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "standard"})
    assert res_round.status_code == status.HTTP_201_CREATED
    round_id = res_round.json()["round_id"]

    # 3. Add question
    res_q = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "Alisher Navoiy qaysi asarning muallifi?",
        "primary_answer": "Xamsa",
        "points": 1,
    })
    assert res_q.status_code == status.HTTP_201_CREATED

    # 4. Read draft details (allowed for owner)
    res_read = client.get(f"/api/drafts/{draft_id}")
    assert res_read.status_code == status.HTTP_200_OK
    assert res_read.json()["total_questions"] == 1

    # 5. Question bank accessible
    res_qb = client.get("/api/questions")
    assert res_qb.status_code == status.HTTP_200_OK

    # 6. Owner panel status accessible
    res_status = client.get("/api/owner/status")
    assert res_status.status_code == status.HTTP_200_OK


def test_password_length_exceeding_72_bytes_fails_safely_not_500(client):
    """
    Regression test: Passwords exceeding bcrypt's 72-byte limit must return
    HTTP 422 Unprocessable Entity (or HTTP 401 on login) rather than unhandled HTTP 500.
    """
    long_password = "A" * 73

    # Registration attempt with 73-byte password -> 422
    res_reg = client.post("/api/auth/register", json={
        "display_name": "Long Password User",
        "email": "longpw@zakowhat.uz",
        "password": long_password,
    })
    assert res_reg.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert res_reg.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR

    # Login attempt with 100-byte password -> 401 (fails safely, not 500)
    res_login = client.post("/api/auth/login", json={
        "email": "longpw@zakowhat.uz",
        "password": "B" * 100,
    })
    assert res_login.status_code == status.HTTP_401_UNAUTHORIZED
    assert res_login.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR


def test_numeric_and_date_answer_typo_safety():
    """
    Regression test: Numeric and date answers must NEVER be matched via typo edit distance.
    1991 is not 1992, and 100000 is not 100001.
    """
    # Distinct years must never match
    assert is_conservative_typo_match("1992-yil", "1991-yil") is False
    assert is_conservative_typo_match("1992", "1991") is False
    assert is_conservative_typo_match("1941 yil", "1945 yil") is False

    # Distinct numbers must never match
    assert is_conservative_typo_match("100001", "100000") is False
    assert is_conservative_typo_match("25", "26") is False

    # Legitimate non-numeric typos still match
    assert is_conservative_typo_match("samarqan", "samarqand") is True


def test_production_owner_email_self_registration_block(client, monkeypatch):
    """
    Regression test: In production, attempting to self-register an account
    using the configured OWNER_EMAIL is strictly blocked with HTTP 403.
    """
    owner_email = "prod_owner@zakowhat.uz"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OWNER_EMAIL", owner_email)

    client.cookies.clear()
    res = client.post("/api/auth/register", json={
        "display_name": "Imposter",
        "email": owner_email,
        "password": "Password123!",
    })
    assert res.status_code == status.HTTP_403_FORBIDDEN
    assert "tizim egasi" in res.json()["detail"].lower()


def test_google_oauth_rejects_unverified_email(client, monkeypatch):
    """
    Regression test: Google OAuth token with email_verified=False must be
    rejected with HTTP 401 Unauthorized to prevent identity spoofing.
    """
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "mock-client-id.apps.googleusercontent.com")

    from unittest.mock import patch
    mock_id_info = {
        "sub": "google-user-12345",
        "email": "unverified@gmail.com",
        "name": "Unverified User",
        "email_verified": False,
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_id_info):
        res = client.post("/api/auth/google", json={"credential": "mock-jwt-token"})
        assert res.status_code == status.HTTP_401_UNAUTHORIZED
        assert "tasdiqlanmagan" in res.json()["detail"].lower()


def test_google_oauth_accepts_verified_email(client, monkeypatch):
    """
    Regression test: Google OAuth token with email_verified=True succeeds with HTTP 200 OK.
    """
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "mock-client-id.apps.googleusercontent.com")

    from unittest.mock import patch
    unique_email = f"verified_{uuid.uuid4().hex[:6]}@gmail.com"
    mock_id_info = {
        "sub": f"google-sub-{uuid.uuid4().hex[:8]}",
        "email": unique_email,
        "name": "Verified Google User",
        "email_verified": True,
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_id_info):
        res = client.post("/api/auth/google", json={"credential": "mock-jwt-token"})
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["success"] is True
        assert data["user"]["email"] == unique_email
