import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_production_dependency_google_auth_requests_importable():
    """
    Regression test: Ensures requests package is installed and usable
    by google.auth.transport.requests for Google Sign-In.
    """
    from google.auth.transport import requests as google_requests
    req = google_requests.Request()
    assert req is not None


def test_production_dependency_psycopg_importable():
    """
    Regression test: Ensures psycopg v3 driver is installed and importable.
    """
    import psycopg
    assert psycopg.__version__ is not None


def test_homepage_renders_grand_sanctum_and_never_old_mvp(client):
    """
    Regression test: Verifies GET / serves Grand Sanctum, not the legacy MVP.
    """
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    
    # Must contain Grand Sanctum signatures
    assert "Daqiqa Ichra Haqiqat Jilvasi" in html
    assert "Mustaqil Intellektual Maydon" in html
    assert "ZakoWhat" in html
    assert "zakowhat-design-system.css" in html
    
    # Must NEVER contain the old MVP foundation text
    assert "Zakovat Platform MVP" not in html
    assert "Фундамент заложен" not in html


def test_homepage_and_arena_have_no_cache_headers(client):
    """
    Regression test: Verifies that HTML pages send Cache-Control headers
    preventing edge proxies and browsers from serving stale cached templates.
    """
    res_home = client.get("/")
    assert "no-cache" in res_home.headers.get("Cache-Control", "")
    assert "no-store" in res_home.headers.get("Cache-Control", "")

    res_arena = client.get("/arena")
    assert "no-cache" in res_arena.headers.get("Cache-Control", "")
    assert "no-store" in res_arena.headers.get("Cache-Control", "")


def test_arena_route_renders_catalog_container(client):
    """
    Verifies GET /arena serves the Arena catalog SPA container.
    """
    response = client.get("/arena")
    assert response.status_code == 200
    html = response.text
    assert "ZAKOWHAT ARENA" in html
    assert "Rasmiy Viktorina To‘plamlari" in html
    assert "Zakovat Platform MVP" not in html


def test_static_assets_load_cleanly(client):
    """
    Verifies that all core static assets required by Grand Sanctum and Arena load with 200 OK.
    """
    css_res = client.get("/static/css/zakowhat-design-system.css")
    assert css_res.status_code == 200
    assert len(css_res.content) > 1000

    insignia_res = client.get("/static/svg/zakowhat-insignia.svg")
    assert insignia_res.status_code == 200
    assert len(insignia_res.content) > 100

    favicon_res = client.get("/static/svg/zakowhat-favicon.svg")
    assert favicon_res.status_code == 200
    assert len(favicon_res.content) > 50

    girih_res = client.get("/static/svg/zakowhat-girih-pattern.svg")
    assert girih_res.status_code == 200
    assert len(girih_res.content) > 500


def test_public_catalog_never_exposes_drafts(client):
    """
    Verifies that GET /api/quizzes strictly returns published quizzes only,
    and draft packs never leak into the public catalog.
    """
    response = client.get("/api/quizzes")
    assert response.status_code == 200
    quizzes = response.json()
    from database import SessionLocal
    from models import QuizVersion
    db = SessionLocal()
    try:
        for q in quizzes:
            v = db.query(QuizVersion).filter(QuizVersion.id == q["version_id"]).first()
            assert v is not None
            assert v.status == "published"
    finally:
        db.close()



def test_owner_panel_endpoints_inaccessible_to_unauthorized(client):
    """
    Verifies that administrative Owner endpoints reject anonymous access with HTTP 401.
    """
    endpoints = [
        "/api/owner/status",
        "/api/owner/quizzes",
        "/api/owner/questions",
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 401


def test_question_bank_inaccessible_to_unauthorized(client):
    """
    Verifies that Question Bank explorer rejects anonymous access with HTTP 401.
    """
    res = client.get("/api/questions")
    assert res.status_code == 401


def test_staging_endpoints_inaccessible_to_unauthorized(client):
    """
    Verifies that Staging endpoints reject anonymous access with HTTP 401.
    """
    res = client.get("/api/play/staging/quizzes")
    assert res.status_code == 401

    res = client.get("/api/play/staging/quizzes/1")
    assert res.status_code == 401

    res = client.post("/api/play/staging/start/1")
    assert res.status_code == 401


def test_arena_renders_catalog_even_with_active_anonymous_attempt(client):
    """
    Regression test for Production UX Bug:
    When an anonymous player has an unfinished/active attempt and navigates to /arena
    (e.g., using browser Back or typing /arena), /arena must ALWAYS render the Arena
    catalog (screen-selection), and NEVER automatically hijack or replace the view with
    the active game. The attempt remains resumable via explicit action ("Davom etish").
    """
    # 1. Fetch available quizzes to get a playable published quiz
    res_quizzes = client.get("/api/quizzes")
    assert res_quizzes.status_code == 200
    quizzes = res_quizzes.json()
    assert len(quizzes) > 0, "At least one published quiz must exist"
    quiz_id = quizzes[0]["quiz_id"]

    # 2. Anonymous player starts the quiz
    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == 201
    start_data = start_res.json()
    session_token = start_data["session_token"]
    assert session_token is not None

    # Verify attempt is in_progress
    state_res = client.get(f"/api/play/{session_token}")
    assert state_res.status_code == 200
    assert state_res.json()["status"] == "in_progress"

    # 3. Anonymous player navigates to /arena (presenting their anonymous cookie)
    arena_res = client.get("/arena")
    assert arena_res.status_code == 200
    html = arena_res.text

    # Contract A: HTML must render the Arena Catalog SPA container (screen-selection)
    assert "ZAKOWHAT ARENA" in html
    assert "Rasmiy Viktorina To‘plamlari" in html
    assert 'id="screen-selection"' in html
    assert 'id="screen-gameplay"' in html

    # Contract B: DOM layout has screen-selection visible by default and screen-gameplay hidden
    assert '<section id="screen-selection" class="space-y-8 animate-fade-in">' in html
    assert '<section id="screen-gameplay" class="hidden animate-fade-in w-full">' in html

    # Contract C: Frontend JavaScript routing guarantees /arena never automatically invokes restoreActiveSession
    # restoreActiveSession on load must be guarded strictly by /play/{id} route
    assert "const playMatch = path.match(/^\\/play\\/(\\d+)$/);" in html
    assert "if (playMatch) {" in html

    # Contract D: Single-page navigation includes popstate listener to return to catalog on browser Back
    assert "window.addEventListener('popstate'" in html
    assert "showScreen('screen-selection')" in html

    # Contract E: Catalog supports explicit resume UX ("Davom etish")
    assert "hasActiveSessionForQuiz" in html
    assert "handleCardPlayAction" in html
    assert "Davom etish" in html

    # Contract F: The active attempt was NOT destroyed by navigating to /arena; it remains resumable
    resume_check = client.get(f"/api/play/{session_token}")
    assert resume_check.status_code == 200
    assert resume_check.json()["status"] == "in_progress"

