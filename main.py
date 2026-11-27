from fastapi import FastAPI, Request, Depends, status, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, Response
import logging
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import os
import hashlib
from functools import lru_cache
from database import test_database_connection, get_db
from api.quizzes import router as quizzes_router
from api.play import router as play_router, verify_staging_access
from api.questions import router as questions_router
from api.drafts import router as drafts_router
from api.owner import router as owner_router, get_owner_emails
from contextlib import asynccontextmanager
from api.auth import router as auth_router, users_router, get_secret_key, get_current_user_optional
from api.daily import router as daily_router
from api.me import router as me_router
from api.demo import router as demo_router
from api.rooms import router as rooms_router, is_room_host, player_join_enabled


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validates SECRET_KEY configuration (fails loudly in production if missing)
    get_secret_key()
    yield


app = FastAPI(title="ZakoWhat API", lifespan=lifespan, debug=False)

@app.middleware("http")
async def security_headers_and_csrf_middleware(request: Request, call_next):
    """
    Applies security headers and enforces origin verification for cross-site state changes.
    """
    # 1. CSRF Protection for state-changing cross-origin requests
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin:
            allowed_origins = [
                # Same host over either scheme: behind a TLS-terminating proxy (e.g. Railway) the app sees
                # http while the browser's Origin is https. A different HOST is still rejected (CSRF).
                f"http://{request.url.netloc}",
                f"https://{request.url.netloc}",
                "http://localhost:8000",
                "http://127.0.0.1:8000",
                "http://localhost:3000",
                "http://testserver",  # Starlette TestClient
            ]
            env_frontend = os.getenv("FRONTEND_URL", "").strip()
            if env_frontend:
                allowed_origins.append(env_frontend.rstrip("/"))
            extra_allowed = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
            allowed_origins.extend(extra_allowed)

            normalized_origin = origin.rstrip("/")
            if not any(normalized_origin == ao.rstrip("/") for ao in allowed_origins):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Cross-site so'rov rad etildi (CSRF protection)"},
                )

    response = await call_next(request)

    # 2. Hardened Security Headers
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # HTTPS only (Railway terminates TLS, so the original scheme arrives in X-Forwarded-Proto). Browsers ignore
    # this header over plain http; no includeSubDomains/preload, so other subdomains are unaffected.
    if request.headers.get("x-forwarded-proto", request.url.scheme) == "https":
        response.headers["Strict-Transport-Security"] = "max-age=15552000"
    if "referrer-policy" not in response.headers:          # a route may ask for a stricter policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://accounts.google.com https://apis.google.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' https://accounts.google.com; "
            "frame-src 'self' https://accounts.google.com;"
        )

    return response


def _is_number_out_of_range(exc: Exception) -> bool:
    """An id in the URL too large for the database integer type (SQLite: OverflowError; PostgreSQL: DataError
    "integer out of range")."""
    from sqlalchemy.exc import DataError, StatementError
    if isinstance(exc, OverflowError):
        return True
    if isinstance(exc, StatementError) and isinstance(getattr(exc, "orig", None), OverflowError):
        return True
    return isinstance(exc, DataError) and "out of range" in str(getattr(exc, "orig", exc)).lower()


from sqlalchemy.exc import DataError as _DataError, StatementError as _StatementError  # noqa: E402
from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def http_error_handler(request: Request, exc: _StarletteHTTPException):
    """A browser asking for a page that does not exist gets a real page, not {"detail": "Not Found"}.
    API clients (and anything not asking for HTML) keep the JSON answer."""
    wants_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == 404 and wants_html and request.method == "GET" and not request.url.path.startswith("/api/"):
        response = templates.TemplateResponse(request=request, name="not_found.html", status_code=404)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex"
        return response
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None))


@app.exception_handler(OverflowError)
@app.exception_handler(_DataError)
@app.exception_handler(_StatementError)
async def out_of_range_handler(request: Request, exc: Exception):
    """e.g. /api/quizzes/99999999999999999999: no row can have that id, so it is "not found", not a server error."""
    if _is_number_out_of_range(exc):
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Topilmadi"})
    return await global_exception_handler(request, exc)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Centralized exception handler: prevents stack traces and server internals
    from leaking to public users while logging errors for observability.
    """
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    logging.getLogger("uvicorn.error").exception(
        "Unhandled exception on %s %s: %s", request.method, request.url.path, exc
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Ichki server xatoligi yuz berdi"},
    )

templates = Jinja2Templates(directory="templates")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@lru_cache(maxsize=None)
def static_url(path: str) -> str:
    """
    Versioned URL for a static asset: /static/<path>?v=<content hash>.
    The hash changes whenever the file's content changes, so browsers fetch the new
    CSS/JS after a deploy instead of reusing a stale cached copy. Computed once per
    process (files do not change while a deployment is running).
    """
    clean = path.lstrip("/")
    try:
        with open(os.path.join(STATIC_DIR, clean), "rb") as f:
            version = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return f"/static/{clean}"
    return f"/static/{clean}?v={version}"


templates.env.globals["static_url"] = static_url

# Absolute public origin for canonical / Open Graph / sitemap URLs (optional PUBLIC_SITE_URL, safe fallback)
from services.daily_engagement import public_site_url  # noqa: E402
templates.env.globals["public_site_url"] = public_site_url
templates.env.globals["rooms_join_enabled"] = player_join_enabled

# Pages that are meant to be found in search. Everything else is either private, user-specific or
# rendered client-side without its own content (see docs/GAMEPLAY_AND_FRONTEND.md, "Indexing").
SITEMAP_PAGES = (
    ("/", "weekly", "1.0"),
    ("/arena", "weekly", "0.9"),
    ("/daily", "daily", "0.8"),
)
# /privacy and /terms stay public but carry <meta name="robots" content="noindex">: not offered to search engines.

ROBOTS_DISALLOW = (
    "/api/", "/owner", "/bank", "/admin", "/builder", "/create", "/staging",
    "/play/", "/profile", "/u/", "/preview/", "/reset-password", "/room/", "/join", "/host", "/verify-email",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(quizzes_router)
app.include_router(play_router)
app.include_router(questions_router)
app.include_router(drafts_router)
app.include_router(owner_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(daily_router)
app.include_router(me_router)
app.include_router(demo_router)
app.include_router(rooms_router)

@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    """Crawling hints only — access control stays on the server."""
    lines = ["User-agent: *", "Allow: /"]
    lines += [f"Disallow: {path}" for path in ROBOTS_DISALLOW]
    lines += ["", f"Sitemap: {public_site_url()}/sitemap.xml", ""]
    return Response("\n".join(lines), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    """Canonical public pages on the public origin (never localhost unless PUBLIC_SITE_URL says so)."""
    from xml.sax.saxutils import escape
    origin = public_site_url()
    entries = "".join(
        f"  <url><loc>{escape(origin + path)}</loc><changefreq>{freq}</changefreq><priority>{prio}</priority></url>\n"
        for path, freq, prio in SITEMAP_PAGES
    )
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + entries + "</urlset>\n")
    return Response(body, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/health")
def health_check():
    """For uptime monitors: 200 only when the site can reach its database, 503 otherwise (a monitor checks the
    status code; the old answer was 200 "unavailable" even with the database down)."""
    db_is_connected = test_database_connection()
    body = {"status": "ok" if db_is_connected else "degraded",
            "database": "connected" if db_is_connected else "unavailable"}
    return JSONResponse(status_code=200 if db_is_connected else 503, content=body,
                        headers={"Cache-Control": "no-store"})

@app.get("/")
def read_grand_sanctum_homepage(request: Request, db: Session = Depends(get_db)):
    # Signed-out visitors get the sample question's answer row blurred with a sign-up invitation.
    viewer_signed_in = get_current_user_optional(request, db) is not None
    response = templates.TemplateResponse(request=request, name="homepage_grand_sanctum.html",
                                          context={"viewer_signed_in": viewer_signed_in})
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.get("/daily")
def read_daily_page(request: Request):
    response = templates.TemplateResponse(request=request, name="daily.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.get("/privacy")
def read_privacy_page(request: Request):
    response = templates.TemplateResponse(request=request, name="legal/privacy.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/reset-password")
def read_reset_password_page(request: Request):
    """Password reset (request a link / set a new password). Never cached, never indexed, never referred."""
    response = templates.TemplateResponse(request=request, name="reset_password.html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/verify-email")
def read_verify_email_page(request: Request):
    """Confirms an added / changed email from the link in the message. Never cached, never indexed, never referred."""
    response = templates.TemplateResponse(request=request, name="verify_email.html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/terms")
def read_terms_page(request: Request):
    response = templates.TemplateResponse(request=request, name="legal/terms.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/arena")
@app.get("/profile")
@app.get("/profile/{user_id_or_username}")
@app.get("/u/{username}")
@app.get("/quiz/{quiz_id}")
@app.get("/play/{quiz_id}")
def read_root(request: Request, quiz_id: str = None, user_id_or_username: str = None, username: str = None):
    # quiz_id is read by the page script; a non-number (/quiz/abc) must still get the page, not a raw JSON error
    response = templates.TemplateResponse(request=request, name="index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/staging")
@app.get("/staging/{quiz_id}")
def read_staging(request: Request, quiz_id: str = None, db: Session = Depends(get_db)):
    """Staff-only page. Everyone else (signed out, or signed in without staging rights) is sent to the public
    Arena. Same rule as the staging API (verify_staging_access), which still enforces access on its own."""
    user = get_current_user_optional(request, db)
    allowed = False
    if user:
        try:
            verify_staging_access(user)
            allowed = True
        except HTTPException:
            allowed = False
    if not allowed:
        return RedirectResponse(url="/arena", status_code=status.HTTP_303_SEE_OTHER,
                                headers={"Cache-Control": "no-store"})
    response = templates.TemplateResponse(request=request, name="index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


def _viewer_is_owner(request: Request, db: Session) -> bool:
    """Same rule as the owner API (verify_owner_access): signed in, and the email is in OWNER_EMAIL(S).
    Fails closed when no owner is configured."""
    user = get_current_user_optional(request, db)
    owner_emails = get_owner_emails()
    return bool(user and owner_emails and user.email.lower() in owner_emails)


def _away_from_owner_pages() -> RedirectResponse:
    """Everyone who is not the owner (signed out or not) is sent to the public Arena, without any hint that an
    owner area exists. The owner signs in on the site as usual and then opens /owner."""
    return RedirectResponse(url="/arena", status_code=status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"})


def _owner_page(request: Request, name: str):
    response = templates.TemplateResponse(request=request, name=name)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/bank")
@app.get("/admin")
def read_bank(request: Request, db: Session = Depends(get_db)):
    if not _viewer_is_owner(request, db):
        return _away_from_owner_pages()
    return _owner_page(request, "bank.html")

@app.get("/builder")
@app.get("/builder/{draft_id}")
@app.get("/create")
def read_builder(request: Request, draft_id: str = None, db: Session = Depends(get_db)):
    if not _viewer_is_owner(request, db):
        return _away_from_owner_pages()
    return _owner_page(request, "builder.html")

@app.get("/owner")
@app.get("/owner/{subpath:path}")
def read_owner_page(request: Request, subpath: str = "", db: Session = Depends(get_db)):
    """Owner panel page. The owner API enforces access on its own; the page itself is no longer shown
    (with its sign-in screen) to anyone else."""
    if not _viewer_is_owner(request, db):
        return _away_from_owner_pages()
    return _owner_page(request, "owner.html")

# --- Jonli xona (live rooms) pages -------------------------------------------------------------------------

def _room_page(request: Request, name: str, code: str = ""):
    from api.rooms import _normalize_code
    response = templates.TemplateResponse(request=request, name=name, context={"room_code": _normalize_code(code)})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/join")
def read_room_join(request: Request):
    """Players enter the room code and a team name. No account needed. Only while phone joining is on."""
    if not player_join_enabled():
        return RedirectResponse(url="/arena", status_code=status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"})
    return _room_page(request, "room_join.html")


@app.get("/room/{code}")
def read_room_player(request: Request, code: str):
    if not player_join_enabled():
        return RedirectResponse(url="/arena", status_code=status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"})
    return _room_page(request, "room_play.html", code)


@app.get("/host")
def read_host_home(request: Request, db: Session = Depends(get_db)):
    """Hosts (owner or HOST_EMAILS): open a room for a pack, see their rooms, how to set up two screens."""
    if not is_room_host(get_current_user_optional(request, db)):
        return _away_from_owner_pages()
    return _room_page(request, "host_home.html")


@app.get("/room/{code}/screen")
def read_room_screen(request: Request, code: str):
    """Projector page: question, clock, answered count, then the answer and the table."""
    return _room_page(request, "room_screen.html", code)


@app.get("/room/{code}/host")
def read_room_host(request: Request, code: str, db: Session = Depends(get_db)):
    if not is_room_host(get_current_user_optional(request, db)):
        return _away_from_owner_pages()
    return _room_page(request, "room_host.html", code)


@app.get("/preview/grand-sanctum")
@app.get("/preview/homepage")
def read_preview_grand_sanctum(request: Request):
    return templates.TemplateResponse(request=request, name="homepage_grand_sanctum.html")


