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
