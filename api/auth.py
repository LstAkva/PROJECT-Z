import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
import bcrypt
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from database import get_db
from models import User, SoloAttempt

router = APIRouter(prefix="/api/auth", tags=["auth"])

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30


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


def hash_password(password: str) -> str:
    """Hashes a plaintext password using bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plaintext password against a bcrypt hash."""
    if not hashed_password:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
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
    auth_provider: str
    created_at: str


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
    clean_email = payload.email.strip().lower()
    if "@" not in clean_email or "." not in clean_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Noto'g'ri email formati",
        )

    clean_name = payload.display_name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
        request_anon_id = request.cookies.get("zakowhat_anon_id") or request.headers.get("X-Anon-Id")
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
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Logs in with email and password, setting HttpOnly auth cookie."""
    clean_email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == clean_email).first()
    if not user or not verify_password(payload.password, user.hashed_password or ""):
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
            "auth_provider": current_user.auth_provider,
            "created_at": current_user.created_at.isoformat(),
        },
    }


@router.post("/google", status_code=status.HTTP_200_OK)
def google_auth(payload: GoogleAuthRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Authenticates via Google Identity Services ID token using the official google-auth library.
    If GOOGLE_CLIENT_ID is not configured, returns clean HTTP 503 error.
    """
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
        request_anon_id = request.cookies.get("zakowhat_anon_id") or request.headers.get("X-Anon-Id")
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
