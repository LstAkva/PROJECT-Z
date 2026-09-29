import os
import urllib.parse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError

# Явно загружаем переменные из файла .env
load_dotenv()


def normalize_database_url(url: str | None) -> str | None:
    """
    Normalizes PostgreSQL connection URL for universal driver compatibility.
    1. Converts legacy postgres:// to postgresql://.
    2. If generic postgresql:// is provided and psycopg2 is missing, falls back to psycopg (v3).
    3. Preserves explicit driver schemes (e.g. postgresql+psycopg://, postgresql+psycopg2://).
    """
    if not url:
        return url
    normalized = url.strip()
    if normalized.startswith("postgres://"):
        normalized = normalized.replace("postgres://", "postgresql://", 1)
    if normalized.startswith("postgresql://"):
        try:
            import psycopg2  # noqa: F401
        except (ImportError, Exception):
            try:
                import psycopg  # noqa: F401
                normalized = normalized.replace("postgresql://", "postgresql+psycopg://", 1)
            except (ImportError, Exception):
                pass
    return normalized


def validate_database_environment(
    url: str | None = None,
    environment: str | None = None,
    db_target: str | None = None,
) -> None:
    """
    Validates database target and environment configuration.
    Guards against accidental connection to production databases from development/test,
    and prevents non-production database usage in production.
    Never logs or exposes credentials or raw connection strings.
    Fails closed: if ENVIRONMENT or DB_TARGET is missing or empty, raises RuntimeError.
    """
    raw_env = environment if environment is not None else os.getenv("ENVIRONMENT")
    if raw_env is None or not str(raw_env).strip():
        raise RuntimeError(
            "Configuration safety violation: ENVIRONMENT must be explicitly configured "
            "('development', 'test', 'staging', or 'production'). Subsystems fail closed when unset."
        )

    raw_target = db_target if db_target is not None else os.getenv("DB_TARGET")
    if raw_target is None or not str(raw_target).strip():
        raise RuntimeError(
            "Configuration safety violation: DB_TARGET must be explicitly configured "
            "('development', 'test', 'staging', or 'production'). Subsystems fail closed when unset."
        )

    env = str(raw_env).strip().lower()
    target = str(raw_target).strip().lower()

    valid_tiers = ("development", "test", "staging", "production")
    if env not in valid_tiers:
        raise RuntimeError(
            f"Configuration safety violation: Invalid ENVIRONMENT '{env}'. Must be one of: {valid_tiers}"
        )
    if target not in valid_tiers:
        raise RuntimeError(
            f"Configuration safety violation: Invalid DB_TARGET '{target}'. Must be one of: {valid_tiers}"
        )

    if env == "production" and target != "production":
        raise RuntimeError("Configuration mismatch: ENVIRONMENT is 'production' but DB_TARGET is not 'production'")

    if env != "production" and target == "production":
        raise RuntimeError("Configuration safety violation: DB_TARGET is 'production' but ENVIRONMENT is not 'production'")

    check_url = normalize_database_url(url or os.getenv("DATABASE_URL") or "") or ""

    if check_url and not check_url.startswith("sqlite"):
        try:
            parsed = urllib.parse.urlparse(check_url)
            hostname = (parsed.hostname or "").lower()
            dbname = (parsed.path or "").lstrip("/").lower()
        except Exception:
            hostname = ""
            dbname = ""

        # 1. Explicit configured host checks (exact equality, not relying on substring heuristics alone)
        known_prod_host = os.getenv("PROD_DATABASE_HOST", "").strip().lower()
        if known_prod_host and hostname == known_prod_host and env != "production":
            raise RuntimeError(
                "Configuration safety violation: non-production environment configured with production database host (PROD_DATABASE_HOST match)"
            )

        known_dev_host = os.getenv("DEV_DATABASE_HOST", "ep-damp-frog-b1y9sc7x-pooler.c-5.eu-central-1.aws.neon.tech").strip().lower()
        if known_dev_host and hostname == known_dev_host and env == "production":
            raise RuntimeError(
                "Configuration safety violation: production environment configured with development database host (DEV_DATABASE_HOST match)"
            )

        # 2. Structural substring / naming markers
        is_prod_db = (
            "-prod" in hostname
            or "production" in hostname
            or "-prod" in dbname
            or "production" in dbname
        )

        if env != "production" and is_prod_db:
            raise RuntimeError(
                "Configuration safety violation: non-production environment configured with production database host or name"
            )

        is_dev_db = (
            "-dev" in hostname
            or "development" in hostname
            or "-dev" in dbname
            or "development" in dbname
        )

        if env == "production" and hostname and is_dev_db:
            raise RuntimeError(
                "Configuration safety violation: production environment configured with development database host"
            )


# Теперь os.getenv гарантированно увидит DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL")

engine = None
SessionLocal = None

DATABASE_URL = normalize_database_url(DATABASE_URL)

if DATABASE_URL:
    validate_database_environment(DATABASE_URL)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def test_database_connection() -> bool:
    """
    Безопасно проверяет связь с БД (SELECT 1).
    Утечка паролей или строки подключения исключена.
    """
    if not engine:
        return False
        
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        return False

def get_db():
    """Provides a transactional database session per request."""
    if SessionLocal is None:
        raise RuntimeError("Database session factory is not configured.")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()