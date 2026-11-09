from fastapi import FastAPI, Request, Depends, status, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
import logging
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import os
from database import test_database_connection, get_db
from api.quizzes import router as quizzes_router
from api.play import router as play_router
from api.questions import router as questions_router
from api.drafts import router as drafts_router
from api.owner import router as owner_router, get_owner_emails
from contextlib import asynccontextmanager
from api.auth import router as auth_router, users_router, get_secret_key, get_current_user_optional
from api.daily import router as daily_router


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
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://accounts.google.com https://apis.google.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' https://accounts.google.com; "
            "frame-src 'self' https://accounts.google.com;"
        )

    return response


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
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(quizzes_router)
app.include_router(play_router)
app.include_router(questions_router)
app.include_router(drafts_router)
app.include_router(owner_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(daily_router)

@app.get("/health")
def health_check():
    db_is_connected = test_database_connection()
    return {
        "status": "ok",
        "database": "connected" if db_is_connected else "unavailable"
    }

@app.get("/")
def read_grand_sanctum_homepage(request: Request):
    response = templates.TemplateResponse(request=request, name="homepage_grand_sanctum.html")
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
@app.get("/staging")
@app.get("/staging/{quiz_id}")
def read_root(request: Request, quiz_id: int = None, user_id_or_username: str = None, username: str = None):
    response = templates.TemplateResponse(request=request, name="index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/bank")
@app.get("/admin")
def read_bank(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    owner_emails = get_owner_emails()
    if not user or not owner_emails or user.email.lower() not in owner_emails:
        return RedirectResponse(url="/owner", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="bank.html")

@app.get("/builder")
@app.get("/builder/{draft_id}")
@app.get("/create")
def read_builder(request: Request, draft_id: int = None, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    owner_emails = get_owner_emails()
    if not user or not owner_emails or user.email.lower() not in owner_emails:
        return RedirectResponse(url="/owner", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="builder.html")

@app.get("/owner")
@app.get("/owner/{subpath:path}")
def read_owner_page(request: Request, subpath: str = ""):
    return templates.TemplateResponse(request=request, name="owner.html")

@app.get("/preview/grand-sanctum")
@app.get("/preview/homepage")
def read_preview_grand_sanctum(request: Request):
    return templates.TemplateResponse(request=request, name="homepage_grand_sanctum.html")


