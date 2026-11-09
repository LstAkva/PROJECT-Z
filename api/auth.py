import html
import os
import time
from collections import defaultdict
import re
import secrets
import base64
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
import bcrypt
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import User, SoloAttempt, QuizVersion, Quiz, AnswerRecord

router = APIRouter(prefix="/api/auth", tags=["auth"])

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30
SESSION_INACTIVITY_LIMIT_SECONDS = 1800  # 30 minutes server-side inactivity limit


def is_practice_attempt(attempt) -> bool:
    """Practice runs are attempts created under the ranked-attempt rule with competitive=False.
    Attempts without the flag (historical) are never treated as practice."""
    return (attempt.attempt_metadata or {}).get("competitive") is False


def get_secret_key() -> str:
    """
    Retrieves the JWT signing secret.
    In production (ENVIRONMENT == 'production'), SECRET_KEY must be explicitly configured;
    fails loudly with a clear RuntimeError if missing.
    A development-only fallback is acceptable only when ENVIRONMENT != 'production'.
    """
    env = os.getenv("ENVIRONMENT", "development").lower()
    secret = os.getenv("SECRET_KEY")
    if not secret:
        if env == "production":
            raise RuntimeError("CRITICAL: SECRET_KEY environment variable must be explicitly configured in production mode.")
        return "zakowhat-dev-only-secret-key-unsafe-for-production"
    return secret


def is_secure_cookie() -> bool:
    """Secure=True in production/HTTPS, Secure=False for local HTTP development."""
    return os.getenv("ENVIRONMENT", "development").lower() == "production"


def get_authoritative_anon_id(request: Request) -> Optional[str]:
    """
    Resolves authoritative anonymous visitor identity.
    Prioritizes server-issued HttpOnly zakowhat_anon_id cookie.
    Client-supplied headers must NOT override or bypass the cookie.
    Only allows X-Anon-Id header when explicitly configured in development/test/local environments,
    or when TESTING=true. Fails closed: if ENVIRONMENT is unset, missing, or production,
    headers are strictly rejected.
    """
    cookie_val = request.cookies.get("zakowhat_anon_id")
    if cookie_val and cookie_val.strip():
        return cookie_val.strip()

    is_testing = os.getenv("TESTING", "").strip().lower() in ("true", "1")
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    is_dev_or_test = is_testing or (env in ("development", "test", "local"))
    if is_dev_or_test:
        header_val = request.headers.get("X-Anon-Id")
        if header_val and header_val.strip():
            return header_val.strip()

    return None


def hash_password(password: str) -> str:
    """Hashes a plaintext password using bcrypt with 72-byte safety guard."""
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > 72:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Parol uzunligi 72 baytdan oshmasligi kerak (Password cannot be longer than 72 bytes)",
        )
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plaintext password against a bcrypt hash with safe 72-byte handling."""
    if not hashed_password:
        return False
    pw_bytes = plain_password.encode("utf-8")
    if len(pw_bytes) > 72:
        return False
    try:
        return bcrypt.checkpw(pw_bytes, hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: int, email: str, expires_delta: Optional[timedelta] = None) -> str:
    """Generates a signed HS256 JWT access token."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, get_secret_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Decodes and verifies a JWT access token."""
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
        return payload
    except Exception:
        return None


def set_auth_cookie(response: Response, token: str):
    """Sets the HttpOnly access_token cookie."""
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie(),
        path="/",
        max_age=ACCESS_TOKEN_EXPIRE_DAYS * 24 * 3600,
    )


def clear_auth_cookie(response: Response):
    """Clears the access_token cookie."""
    response.delete_cookie(
        key="access_token",
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie(),
        path="/",
    )


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """
    Returns the currently authenticated User or None if unauthenticated/anonymous.
    Checks Authorization header first, then HttpOnly access_token cookie.
    """
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "access_token" in request.cookies:
        token = request.cookies.get("access_token")

    if not token:
        return None

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None

    try:
        user_id = int(payload["sub"])
    except (ValueError, TypeError):
        return None

    return db.query(User).filter(User.id == user_id).first()


def get_current_user_required(current_user: Optional[User] = Depends(get_current_user_optional)) -> User:
    """Enforces authentication; raises HTTP 401 if unauthenticated."""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tizimga kirish talab qilinadi",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


_RATE_LIMIT_BUCKETS: Dict[str, list] = defaultdict(list)


def enforce_rate_limit(request: Request, key_prefix: str, max_requests: int = 15, window_seconds: int = 60) -> None:
    """
    In-memory sliding-window rate limiter for sensitive endpoints.
    Bypassed in test environment (ENVIRONMENT == 'test' or TESTING == 'true').
    """
    if os.getenv("ENVIRONMENT") == "test" or os.getenv("TESTING") == "true":
        return

    cf_ip = request.headers.get("CF-Connecting-IP")
    real_ip = request.headers.get("X-Real-IP")
    forwarded = request.headers.get("X-Forwarded-For")
    if cf_ip:
        ip = cf_ip.strip()
    elif real_ip:
        ip = real_ip.strip()
    elif forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"

    bucket_key = f"{key_prefix}:{ip}"
    now = time.time()
    cutoff = now - window_seconds

    # Bounded memory cleanup if dictionary exceeds threshold
    if len(_RATE_LIMIT_BUCKETS) > 1000:
        stale_keys = [k for k, ts in _RATE_LIMIT_BUCKETS.items() if not ts or ts[-1] <= cutoff]
        for k in stale_keys:
            _RATE_LIMIT_BUCKETS.pop(k, None)

    timestamps = [t for t in _RATE_LIMIT_BUCKETS[bucket_key] if t > cutoff]
    if len(timestamps) >= max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Juda ko'p urinishlar qilindi. Iltimos, birozdan keyin qayta urinib ko'ring (Too many requests. Please try again later).",
        )
    timestamps.append(now)
    _RATE_LIMIT_BUCKETS[bucket_key] = timestamps


def get_owner_emails() -> List[str]:
    """
    Reads configured owner email addresses from environment variables.
    Supports OWNER_EMAIL (singular) and OWNER_EMAILS (plural).
    Strictly isolated: does NOT fall back to ADMIN_EMAILS or staging configs.
    Never hardcodes owner credentials.
    """
    raw_singular = os.getenv("OWNER_EMAIL", "").strip()
    raw_plural = os.getenv("OWNER_EMAILS", "").strip()
    emails = []
    if raw_singular:
        emails.extend([e.strip().lower() for e in raw_singular.split(",") if e.strip()])
    if raw_plural:
        emails.extend([e.strip().lower() for e in raw_plural.split(",") if e.strip()])
    return list(dict.fromkeys(emails))


def verify_owner_access(
    current_user: User = Depends(get_current_user_required),
) -> User:
    """
    Authoritative server-side owner authorization guard.
    - 401 Unauthorized if unauthenticated (enforced by get_current_user_required).
    - 403 Forbidden (FAIL CLOSED) if OWNER_EMAIL / OWNER_EMAILS is missing or empty.
    - 403 Forbidden if the authenticated user's email is not in the owner list.
    - Returns current_user if authorized.
    """
    owner_emails = get_owner_emails()
    if not owner_emails:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tizim egasi (Owner) konfiguratsiyasi mavjud emas. Kirish taqiqlangan.",
        )

    if current_user.email.lower() not in owner_emails:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ushbu amal faqat tizim egasi (Owner) uchun ruxsat etilgan.",
        )

    return current_user


# =========================================================
# SCHEMAS
# =========================================================

class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)
    display_name: str = Field(..., min_length=1, max_length=100)
    session_token: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class GoogleAuthRequest(BaseModel):
    credential: str
    session_token: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    username: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    is_public: bool = True
    show_stats: bool = True
    theme_preference: str = "dark"
    cookie_consent: str = "necessary"
    auth_provider: str
    created_at: str


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    username: Optional[str] = Field(None, max_length=50)
    bio: Optional[str] = Field(None, max_length=500)
    is_public: Optional[bool] = None
    show_stats: Optional[bool] = None
    theme_preference: Optional[str] = None
    cookie_consent: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


class ConsentRequest(BaseModel):
    cookie_consent: str = Field(..., pattern="^(necessary|all|minimal)$")


class AvatarUploadRequest(BaseModel):
    image_data: str = Field(..., description="Base64 encoded image string or data URI")


class DeleteAccountRequest(BaseModel):
    confirm_text: str


# =========================================================
# ENDPOINTS
# =========================================================

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Registers a new account.
    If session_token of a completed anonymous attempt is passed,
    validates that the attempt belongs to the request's authoritative anon_id,
    has user_id IS NULL, and status == 'completed' before assigning attempt.user_id.
    """
    enforce_rate_limit(request, "register", max_requests=10, window_seconds=60)

    pw_bytes = payload.password.encode("utf-8")
    if len(pw_bytes) > 72:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Parol 72 baytdan oshmasligi kerak (Password cannot be longer than 72 bytes)",
        )

    clean_email = payload.email.strip().lower()
    if os.getenv("ENVIRONMENT") not in ("test", "testing"):
        owner_emails = get_owner_emails()
        if clean_email in owner_emails:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tizim egasi (Owner) hisobi ommaviy ro'yxatdan o'tish orqali ochilishi taqiqlangan.",
            )

    if "@" not in clean_email or "." not in clean_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Noto'g'ri email formati",
        )

    clean_name = html.escape(payload.display_name.strip(), quote=False)
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Ism (display name) kiritilishi shart",
        )

    # Check email uniqueness
    existing = db.query(User).filter(User.email == clean_email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bu email bilan foydalanuvchi allaqachon mavjud",
        )

    # Validate anonymous attempt claim ownership if session_token provided
    claimed_attempt = None
    if payload.session_token:
        token_str = payload.session_token.strip()
        attempt = (
            db.query(SoloAttempt)
            .filter(SoloAttempt.session_token == token_str)
            .first()
        )
        if not attempt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Biriktirilishi so'ralgan o'yin sessiyasi topilmadi",
            )
        request_anon_id = get_authoritative_anon_id(request)
        if (
            not request_anon_id
            or attempt.anon_id != request_anon_id
            or attempt.user_id is not None
            or attempt.status != "completed"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ushbu o'yin sessiyasini biriktirish imkonsiz: anonim egasi mos kelmadi, sessiya allaqachon biriktirilgan yoki o'yin to'liq yakunlanmagan.",
            )
        claimed_attempt = attempt

    # Hash password
    pwd_hash = hash_password(payload.password)

    # Create user
    user = User(
        email=clean_email,
        hashed_password=pwd_hash,
        display_name=clean_name,
        auth_provider="local",
    )
    db.add(user)
    db.flush()

    if claimed_attempt:
        claimed_attempt.user_id = user.id

    db.commit()
    db.refresh(user)

    # Issue token and set HttpOnly cookie
    token = create_access_token(user.id, user.email)
    set_auth_cookie(response, token)

    return {
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "created_at": user.created_at.isoformat(),
        },
        "attempt_claimed": claimed_attempt is not None,
    }


@router.post("/login", status_code=status.HTTP_200_OK)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, "login", max_requests=25, window_seconds=60)
    """Logs in with email or username (display_name) and password, setting HttpOnly auth cookie."""
    clean_identifier = payload.email.strip().lower()
    if not clean_identifier:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email yoki parol noto'g'ri",
        )

    # 1. Authoritative lookup by unique email first
    user = db.query(User).filter(func.lower(User.email) == clean_identifier).first()

    # 2. Fallback lookup by username / display_name
    if not user:
        user = (
            db.query(User)
            .filter(func.lower(User.display_name) == clean_identifier)
            .order_by(User.id.asc())
            .first()
        )

    if not user:
        # Mitigate timing-based account enumeration by equalizing bcrypt execution time
        DUMMY_BCRYPT_HASH = "$2b$12$e8rGgXkQeFq1o3uEw7r9uOpZ1z0v2w4x6y8z0a1b2c3d4e5f6g7h8"
        verify_password(payload.password, DUMMY_BCRYPT_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email yoki parol noto'g'ri",
        )

    if not verify_password(payload.password, user.hashed_password or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email yoki parol noto'g'ri",
        )

    token = create_access_token(user.id, user.email)
    set_auth_cookie(response, token)

    return {
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "created_at": user.created_at.isoformat(),
        },
    }


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(response: Response):
    """Logs out by clearing the HttpOnly auth cookie."""
    clear_auth_cookie(response)
    return {"success": True, "message": "Muvaffaqiyatli tizimdan chiqildi"}


@router.get("/config", status_code=status.HTTP_200_OK)
def get_auth_config():
    """Returns public auth configurations (e.g. Google Client ID for GIS button rendering)."""
    return {
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", "")
    }


@router.get("/me", status_code=status.HTTP_200_OK)
def get_current_user_profile(
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Returns current authentication state and profile information."""
    if not current_user:
        return {"authenticated": False, "user": None}

    return {
        "authenticated": True,
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "display_name": current_user.display_name,
            "username": current_user.username,
            "avatar_url": current_user.avatar_url,
            "bio": current_user.bio,
            "is_public": current_user.is_public if current_user.is_public is not None else True,
            "show_stats": current_user.show_stats if current_user.show_stats is not None else True,
            "theme_preference": current_user.theme_preference or "dark",
            "cookie_consent": current_user.cookie_consent or "necessary",
            "auth_provider": current_user.auth_provider,
            "created_at": current_user.created_at.isoformat(),
        },
    }


@router.get("/profile", status_code=status.HTTP_200_OK)
def get_player_profile(
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """
    Returns personal profile details and persistent progress summary for the authenticated player.
    Excludes creator preview attempts from statistics.
    """
    attempts = (
        db.query(SoloAttempt)
        .options(
            joinedload(SoloAttempt.quiz_version).joinedload(QuizVersion.quiz)
        )
        .filter(
            SoloAttempt.user_id == current_user.id,
            SoloAttempt.timer_mode.notin_(["preview", "preview_no_timer"]),
        )
        .order_by(SoloAttempt.id.desc())
        .all()
    )

    # Sync and persist expired status for inactive sessions (> 1800 seconds / 30 mins of inactivity)
    now = datetime.now(timezone.utc)
    has_status_updates = False
    for a in attempts:
        if a.status in ("in_progress", "board_selection", "intermediate_break"):
            meta = a.attempt_metadata or {}
            last_active_str = meta.get("last_active_at")
            last_active = None
            if last_active_str:
                try:
                    last_active = datetime.fromisoformat(last_active_str)
                except Exception:
                    pass
            elif a.started_at:
                last_active = a.started_at

            if last_active:
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=timezone.utc)
                if (now - last_active).total_seconds() > SESSION_INACTIVITY_LIMIT_SECONDS:
                    a.status = "expired"
                    has_status_updates = True
    if has_status_updates:
        db.commit()

    # Competitive stats exclude practice runs (attempts created under the ranked-attempt rule with
    # competitive=False). Historical attempts (no flag) keep counting exactly as before; practice runs
    # stay in the game history and are reported separately, never silently dropped.
    all_completed = [a for a in attempts if a.status == "completed"]
    completed_attempts = [a for a in all_completed if not is_practice_attempt(a)]
    practice_games = len(all_completed) - len(completed_attempts)
    total_games = len(completed_attempts)
    total_score = sum(a.total_score for a in completed_attempts)
    total_correct = sum(a.total_correct for a in completed_attempts)
    total_time_seconds = sum(a.active_time_seconds for a in completed_attempts)

    competitive_ids = [a.id for a in completed_attempts]
    total_answered_records = (
        db.query(func.count(AnswerRecord.id))
        .filter(AnswerRecord.attempt_id.in_(competitive_ids))
        .scalar() or 0
    ) if competitive_ids else 0

    accuracy_percent = (
        round((total_correct / total_answered_records) * 100, 1)
        if total_answered_records > 0
        else 0.0
    )

    recent_games = []
    for a in attempts[:25]:
        title = "Noma'lum viktorina"
        if a.quiz_version:
            if a.quiz_version.published_manifest and a.quiz_version.published_manifest.get("title"):
                title = a.quiz_version.published_manifest.get("title")
            elif a.quiz_version.quiz:
                title = a.quiz_version.quiz.title

        recent_games.append({
            "id": a.id,
            "session_token": a.session_token,
            "quiz_id": a.quiz_version.quiz_id if a.quiz_version else None,
            "quiz_title": title,
            "game_mode": a.quiz_version.game_mode if a.quiz_version else "classic_zakovat",
            "total_score": a.total_score,
            "total_correct": a.total_correct,
            "status": a.status,
            "timer_mode": a.timer_mode,
            "ranked": not is_practice_attempt(a),
            "active_time_seconds": a.active_time_seconds,
            "started_at": a.started_at.isoformat() if a.started_at else None,
            "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        })

    return {
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "display_name": current_user.display_name,
            "username": current_user.username,
            "avatar_url": current_user.avatar_url,
            "bio": current_user.bio,
            "is_public": current_user.is_public if current_user.is_public is not None else True,
            "show_stats": current_user.show_stats if current_user.show_stats is not None else True,
            "theme_preference": current_user.theme_preference or "dark",
            "cookie_consent": current_user.cookie_consent or "necessary",
            "auth_provider": current_user.auth_provider,
            "created_at": current_user.created_at.isoformat(),
        },
        "stats": {
            "total_games": total_games,
            "total_score": total_score,
            "total_correct": total_correct,
            "total_time_seconds": total_time_seconds,
            "total_questions_answered": total_answered_records,
            "accuracy_percent": accuracy_percent,
            "practice_games": practice_games,
        },
        "recent_games": recent_games,
    }


@router.patch("/profile", status_code=status.HTTP_200_OK)
def update_profile(
    payload: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Updates profile attributes for the authenticated user."""
    if payload.display_name is not None:
        dn = html.escape(payload.display_name.strip(), quote=False)
        if not dn:
            raise HTTPException(status_code=400, detail="Ko'rsatiladigan ism bo'sh bo'lishi mumkin emas")
        current_user.display_name = dn

    if payload.username is not None:
        raw_u = payload.username.strip().lower()
        if raw_u:
            if not re.match(r"^[a-z0-9_]{3,30}$", raw_u):
                raise HTTPException(
                    status_code=400,
                    detail="Foydalanuvchi nomi faqat 3-30 ta lotin harflari, raqamlar va pastki chiziqdan iborat bo'lishi kerak",
                )
            existing = (
                db.query(User)
                .filter(func.lower(User.username) == raw_u, User.id != current_user.id)
                .first()
            )
            if existing:
                raise HTTPException(status_code=400, detail="Ushbu foydalanuvchi nomi band qilingan")
            current_user.username = raw_u
        else:
            current_user.username = None

    if payload.bio is not None:
        raw_bio = html.escape(payload.bio.strip(), quote=False)
        current_user.bio = raw_bio[:500] if raw_bio else None

    if payload.is_public is not None:
        current_user.is_public = bool(payload.is_public)

    if payload.show_stats is not None:
        current_user.show_stats = bool(payload.show_stats)

    if payload.theme_preference is not None:
        pref = payload.theme_preference.strip().lower()
        if pref in ("dark", "light"):
            current_user.theme_preference = pref

    if payload.cookie_consent is not None:
        consent = payload.cookie_consent.strip().lower()
        if consent in ("necessary", "all", "minimal"):
            current_user.cookie_consent = consent
            current_user.consent_recorded_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(current_user)
    return {
        "success": True,
        "message": "Profil muvaffaqiyatli saqlandi",
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "display_name": current_user.display_name,
            "username": current_user.username,
            "avatar_url": current_user.avatar_url,
            "bio": current_user.bio,
            "is_public": current_user.is_public,
            "show_stats": current_user.show_stats,
            "theme_preference": current_user.theme_preference,
            "cookie_consent": current_user.cookie_consent,
        },
    }


@router.post("/avatar", status_code=status.HTTP_200_OK)
def upload_avatar(
    payload: AvatarUploadRequest,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Uploads and validates a player avatar image (PNG, JPG, WebP up to 2MB) via base64."""
    raw_b64 = payload.image_data.strip()
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]

    try:
        contents = base64.b64decode(raw_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Noto'g'ri base64 rasm formati")

    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Rasm hajmi 2 MB dan oshmasligi kerak")
    if len(contents) < 16:
        raise HTTPException(status_code=400, detail="Yaroqsiz rasm fayli")

    ext = None
    if contents.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif contents.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif contents.startswith(b"RIFF") and len(contents) >= 12 and contents[8:12] == b"WEBP":
        ext = "webp"
    elif contents.startswith(b"GIF87a") or contents.startswith(b"GIF89a"):
        ext = "gif"

    if not ext:
        raise HTTPException(status_code=400, detail="Faqat PNG, JPG yoki WebP formatidagi rasmlar qabul qilinadi")

    os.makedirs(os.path.join("static", "uploads", "avatars"), exist_ok=True)
    filename = f"avatar_u{current_user.id}_{secrets.token_hex(6)}.{ext}"
    filepath = os.path.join("static", "uploads", "avatars", filename)

    with open(filepath, "wb") as f:
        f.write(contents)

    # Delete old local avatar file if existing
    if current_user.avatar_url and current_user.avatar_url.startswith("/static/uploads/avatars/"):
        old_path = current_user.avatar_url.lstrip("/")
        if os.path.exists(old_path) and os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass

    current_user.avatar_url = f"/static/uploads/avatars/{filename}"
    db.commit()
    db.refresh(current_user)

    return {
        "success": True,
        "message": "Avatar muvaffaqiyatli yuklandi",
        "avatar_url": current_user.avatar_url,
    }


@router.delete("/avatar", status_code=status.HTTP_200_OK)
def delete_avatar(
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Removes current player avatar."""
    if current_user.avatar_url and current_user.avatar_url.startswith("/static/uploads/avatars/"):
        old_path = current_user.avatar_url.lstrip("/")
        if os.path.exists(old_path) and os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass

    current_user.avatar_url = None
    db.commit()
    return {"success": True, "avatar_url": None, "message": "Avatar o'chirildi"}


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "change_password", max_requests=10, window_seconds=60)
    """Changes password for local auth users with verification of current password."""
    if current_user.auth_provider != "local" or not current_user.hashed_password:
        raise HTTPException(status_code=400, detail="Google akkauntlar uchun parol o'zgartirish mavjud emas")

    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Hozirgi parol noto'g'ri kiritildi")

    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Yangi parol kamida 6 belgidan iborat bo'lishi kerak")

    current_user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"success": True, "message": "Parol muvaffaqiyatli o'zgartirildi"}


@router.post("/consent", status_code=status.HTTP_200_OK)
def record_consent(
    payload: ConsentRequest,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """Persists cookie and privacy preferences."""
    if current_user:
        current_user.cookie_consent = payload.cookie_consent
        current_user.consent_recorded_at = datetime.now(timezone.utc)
        db.commit()
    return {"success": True, "cookie_consent": payload.cookie_consent}


@router.delete("/account", status_code=status.HTTP_200_OK)
def delete_account(
    payload: DeleteAccountRequest,
    response: Response,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Deletes user account and associated private data upon explicit confirmation."""
    confirm = payload.confirm_text.strip().lower()
    valid_confirms = {"delete", "ochirish", "o'chirish", current_user.email.strip().lower()}
    if confirm not in valid_confirms:
        raise HTTPException(
            status_code=400,
            detail="Akkauntni o'chirish uchun tasdiqlash so'zi ('DELETE' yoki emailingiz) noto'g'ri kiritildi",
        )

    # Track avatar file for post-commit cleanup to guarantee transactional consistency
    avatar_to_delete = None
    if current_user.avatar_url and current_user.avatar_url.startswith("/static/uploads/avatars/"):
        old_path = current_user.avatar_url.lstrip("/")
        if os.path.exists(old_path) and os.path.isfile(old_path):
            avatar_to_delete = old_path

    # Anonymize solo attempts so historical statistics preserve leaderboards integrity without PII
    db.query(SoloAttempt).filter(SoloAttempt.user_id == current_user.id).update({"user_id": None})

    # Clean daily submissions
    try:
        from models import DailySubmission
        db.query(DailySubmission).filter(DailySubmission.user_id == current_user.id).delete()
    except Exception:
        pass

    db.delete(current_user)
    db.commit()

    # Clean avatar file only after database transaction commits successfully
    if avatar_to_delete:
        try:
            os.remove(avatar_to_delete)
        except Exception:
            pass

    clear_auth_cookie(response)
    return {"success": True, "message": "Akkauntingiz muvaffaqiyatli o'chirildi"}


# Public profile router & handler
users_router = APIRouter(prefix="/api/users", tags=["users"])


def resolve_public_user_profile(identifier: str, db: Session):
    clean_id = identifier.strip()
    user = None
    if clean_id.isdigit():
        user = db.query(User).filter(User.id == int(clean_id)).first()
    if not user:
        user = db.query(User).filter(func.lower(User.username) == clean_id.lower()).first()

    if not user:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")

    is_public = user.is_public if user.is_public is not None else True
    if not is_public:
        return {
            "id": user.id,
            "display_name": user.display_name,
            "username": user.username,
            "avatar_url": user.avatar_url,
            "is_private": True,
            "show_stats": False,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "stats": None,
        }

    show_stats = user.show_stats if user.show_stats is not None else True
    stats = None
    if show_stats:
        all_completed = (
            db.query(SoloAttempt)
            .filter(
                SoloAttempt.user_id == user.id,
                SoloAttempt.status == "completed",
                SoloAttempt.timer_mode.notin_(["preview", "preview_no_timer"]),
            )
            .all()
        )
        # Public competitive stats exclude practice runs; historical attempts count as before
        completed = [a for a in all_completed if not is_practice_attempt(a)]
        total_games = len(completed)
        total_score = sum(a.total_score for a in completed)
        total_correct = sum(a.total_correct for a in completed)
        competitive_ids = [a.id for a in completed]
        total_answered = (
            db.query(func.count(AnswerRecord.id))
            .filter(AnswerRecord.attempt_id.in_(competitive_ids))
            .scalar() or 0
        ) if competitive_ids else 0
        accuracy = round((total_correct / total_answered) * 100, 1) if total_answered > 0 else 0.0
        stats = {
            "total_games": total_games,
            "total_score": total_score,
            "total_correct": total_correct,
            "accuracy_percent": accuracy,
            "practice_games": len(all_completed) - len(completed),
        }

    return {
        "id": user.id,
        "display_name": user.display_name,
        "username": user.username,
        "avatar_url": user.avatar_url,
        "bio": user.bio,
        "is_private": False,
        "show_stats": show_stats,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "stats": stats,
    }


@users_router.get("/{identifier}/public", status_code=status.HTTP_200_OK)
def get_user_public_profile(identifier: str, db: Session = Depends(get_db)):
    return resolve_public_user_profile(identifier, db)


@router.get("/users/{identifier}/public", status_code=status.HTTP_200_OK)
def get_auth_user_public_profile(identifier: str, db: Session = Depends(get_db)):
    return resolve_public_user_profile(identifier, db)


@router.post("/google", status_code=status.HTTP_200_OK)
def google_auth(payload: GoogleAuthRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Authenticates via Google Identity Services ID token using the official google-auth library.
    If GOOGLE_CLIENT_ID is not configured, returns clean HTTP 503 error.
    """
    enforce_rate_limit(request, "google_auth", max_requests=20, window_seconds=60)
    google_client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Sign-In serverda sozlanmagan (GOOGLE_CLIENT_ID mavjud emas)",
        )

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests

        id_info = id_token.verify_oauth2_token(
            payload.credential,
            google_requests.Request(),
            audience=google_client_id,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Google token verification failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google orqali autentifikatsiya muvaffaqiyatsiz bo'ldi",
        )

    google_id = id_info.get("sub")
    email = (id_info.get("email") or "").strip().lower()
    name = (id_info.get("name") or email.split("@")[0] or "Foydalanuvchi").strip()

    if not google_id or not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google tokenida kerakli foydalanuvchi ma'lumotlari topilmadi",
        )

    # Cryptographic verification: Google ID token must confirm email_verified is True
    if not id_info.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google hisobidagi email manzili tasdiqlanmagan (Email address is not verified by Google)",
        )

    # Validate anonymous attempt claim ownership if session_token provided
    claimed_attempt = None
    if payload.session_token:
        token_str = payload.session_token.strip()
        attempt = (
            db.query(SoloAttempt)
            .filter(SoloAttempt.session_token == token_str)
            .first()
        )
        if not attempt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Biriktirilishi so'ralgan o'yin sessiyasi topilmadi",
            )
        request_anon_id = get_authoritative_anon_id(request)
        if (
            not request_anon_id
            or attempt.anon_id != request_anon_id
            or attempt.user_id is not None
            or attempt.status != "completed"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ushbu o'yin sessiyasini biriktirish imkonsiz: anonim egasi mos kelmadi, sessiya allaqachon biriktirilgan yoki o'yin to'liq yakunlanmagan.",
            )
        claimed_attempt = attempt

    # Find existing user by google_id or email
    user = (
        db.query(User)
        .filter((User.google_id == google_id) | (User.email == email))
        .first()
    )

    if not user:
        user = User(
            email=email,
            hashed_password=None,
            display_name=name,
            auth_provider="google",
            google_id=google_id,
        )
        db.add(user)
        db.flush()
    else:
        if not user.google_id:
            user.google_id = google_id
            user.auth_provider = "google"

    if claimed_attempt:
        claimed_attempt.user_id = user.id

    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.email)
    set_auth_cookie(response, token)

    return {
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "created_at": user.created_at.isoformat(),
        },
        "attempt_claimed": claimed_attempt is not None,
    }
