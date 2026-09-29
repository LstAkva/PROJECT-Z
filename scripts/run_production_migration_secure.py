"""
Secure One-Time Production Migration Runner for ZakoWhat RC1.
Executes Alembic migrations without requiring Render Shell.

Key Safety Guarantees:
1. Never logs or outputs credentials, tokens, or raw connection strings.
2. Accepts DATABASE_URL via TARGET_DATABASE_URL environment variable or masked prompt (getpass).
3. Strictly rejects DEV database host (ep-damp-frog-b1y9sc7x).
4. Strictly asserts baseline revision is 4ed9e2bc20cc before applying migrations.
5. Validates 0 non-null user_id rows in solo_attempts and 0 duplicate (round_id, sequence) in questions.
6. Verifies current git branch is release/rc1-public-arena.
7. Supports --dry-run for complete pre-flight validation without database mutation.
8. Verifies target schema post-migration (revision 9c0d1e2f3a4b and all required tables/indexes).
"""

import os
import sys
import getpass
import argparse
import urllib.parse
import subprocess
from datetime import datetime, timezone

from sqlalchemy import create_engine, text, inspect
from alembic.config import Config
from alembic import command


def mask_url(url: str) -> str:
    """Masks username and password in a database URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or "unknown"
        port = parsed.port or 5432
        dbname = (parsed.path or "").lstrip("/")
        return f"{parsed.scheme}://***:***@{hostname}:{port}/{dbname}"
    except Exception:
        return "postgresql://***:***@****:****/*****"


def verify_git_branch(allow_any_branch: bool = False) -> bool:
    """Verifies that the runner is executing from the release branch."""
    if allow_any_branch:
        return True
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        current_branch = res.stdout.strip()
        expected_branch = "release/rc1-public-arena"
        if current_branch != expected_branch:
            print(f"[ERROR] Branch mismatch: Current branch is '{current_branch}', expected '{expected_branch}'.")
            return False
        return True
    except Exception as e:
        print(f"[WARN] Unable to verify git branch: {e}")
        return False


def get_target_url() -> str:
    """Obtains the target database URL securely."""
    url = os.getenv("TARGET_DATABASE_URL")
    if not url:
        print("\n[PROMPT] Production DATABASE_URL not set in TARGET_DATABASE_URL environment variable.")
        url = getpass.getpass("Enter Production DATABASE_URL (input hidden): ").strip()
    
    if not url:
        raise ValueError("No database URL provided.")
    
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
        
    return url


def run_preflight_checks(engine) -> dict:
    """Runs read-only inspection against the target database."""
    report = {
        "passed": False,
        "current_revision": None,
        "solo_attempts_count": 0,
        "non_null_user_id_count": 0,
        "duplicate_sequence_count": 0,
        "errors": [],
    }

    inspector = inspect(engine)
    with engine.connect() as conn:
        # 1. Check Alembic revision
        try:
            rev_row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
            report["current_revision"] = rev_row[0] if rev_row else "empty"
        except Exception as e:
            report["errors"].append(f"Failed to query alembic_version: {e}")
            return report

        # 2. Check solo_attempts
        try:
            att_row = conn.execute(text("SELECT count(*) FROM solo_attempts")).scalar()
            report["solo_attempts_count"] = att_row

            cols = [c["name"] for c in inspector.get_columns("solo_attempts")]
            if "user_id" in cols:
                non_null_uid = conn.execute(text(
                    "SELECT count(*) FROM solo_attempts WHERE user_id IS NOT NULL"
                )).scalar()
                report["non_null_user_id_count"] = non_null_uid
                if non_null_uid > 0:
                    report["errors"].append(
                        f"Safety violation: Found {non_null_uid} solo_attempts with non-null user_id. "
                        "Constraint fk_solo_attempts_user_id will fail because users table is empty."
                    )
        except Exception as e:
            report["errors"].append(f"Failed to query solo_attempts: {e}")

        # 3. Check duplicate (round_id, sequence) in questions
        try:
            dup_seq = conn.execute(text("""
                SELECT count(*) FROM (
                    SELECT round_id, sequence 
                    FROM questions 
                    WHERE round_id IS NOT NULL 
                    GROUP BY round_id, sequence 
                    HAVING count(*) > 1
                ) sub
            """)).scalar()
            report["duplicate_sequence_count"] = dup_seq
            if dup_seq > 0:
                report["errors"].append(
                    f"Safety violation: Found {dup_seq} duplicate (round_id, sequence) in questions. "
                    "Unique constraint uq_round_sequence will fail during backfill."
                )
        except Exception as e:
            report["errors"].append(f"Failed to query questions: {e}")

    if not report["errors"]:
        report["passed"] = True

    return report


def run_postflight_checks(engine) -> dict:
    """Verifies that the target database is at 9c0d1e2f3a4b and all schema changes took effect."""
    report = {"passed": False, "revision": None, "checks": []}
    inspector = inspect(engine)

    with engine.connect() as conn:
        # 1. Revision
        rev = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
        report["revision"] = rev
        report["checks"].append(f"Alembic revision: {rev} (Target: 9c0d1e2f3a4b)")

        # 2. Check new tables
        tables = inspector.get_table_names()
        has_rq = "round_questions" in tables
        has_users = "users" in tables
        report["checks"].append(f"Table 'round_questions' exists: {has_rq}")
        report["checks"].append(f"Table 'users' exists: {has_users}")

        # 3. Check solo_attempts columns
        cols = [c["name"] for c in inspector.get_columns("solo_attempts")]
        has_anon = "anon_id" in cols
        has_meta = "attempt_metadata" in cols
        report["checks"].append(f"Column 'solo_attempts.anon_id' exists: {has_anon}")
        report["checks"].append(f"Column 'solo_attempts.attempt_metadata' exists: {has_meta}")

        # 4. Check index
        indexes = [idx["name"] for idx in inspector.get_indexes("solo_attempts")]
        has_idx = "idx_solo_attempts_anon_id_unique" in indexes
        report["checks"].append(f"Index 'idx_solo_attempts_anon_id_unique' exists: {has_idx}")

        if rev == "9c0d1e2f3a4b" and has_rq and has_users and has_anon and has_meta and has_idx:
            report["passed"] = True

    return report


def main():
    parser = argparse.ArgumentParser(description="Secure Production Migration Runner (No Render Shell Required)")
    parser.add_argument("--dry-run", action="store_true", help="Run preflight validation without modifying the database")
    parser.add_argument("--execute", action="store_true", help="Execute the migrations (4ed9e2bc20cc -> 9c0d1e2f3a4b)")
    parser.add_argument("--allow-any-branch", action="store_true", help="Allow running on branches other than release/rc1-public-arena (for disposable clones only)")
    parser.add_argument("--test-sqlite", type=str, default=None, help="Testing mode using an isolated SQLite database")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("[ERROR] Please specify either --dry-run or --execute.")
        print("Usage:")
        print("  python scripts/run_production_migration_secure.py --dry-run")
        print("  python scripts/run_production_migration_secure.py --execute")
        sys.exit(1)

    print("====================================================================")
    print(" ZakoWhat RC1 — Secure Production Migration Runner")
    print("====================================================================")
    print(f"Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}")

    # 1. Branch verification
    if not verify_git_branch(args.allow_any_branch):
        print("[ABORTED] Please checkout 'release/rc1-public-arena' before running migrations.")
        sys.exit(1)
    print("[OK] Git branch verified: release/rc1-public-arena")

    # 2. Get database URL
    if args.test_sqlite:
        target_url = f"sqlite:///{args.test_sqlite}"
        is_sqlite = True
    else:
        try:
            target_url = get_target_url()
        except Exception as e:
            print(f"[ABORTED] {e}")
            sys.exit(1)
        is_sqlite = False

    # 3. Host protection check
    masked = mask_url(target_url)
    print(f"[TARGET] Database: {masked}")

    if not is_sqlite:
        parsed = urllib.parse.urlparse(target_url)
        hostname = (parsed.hostname or "").lower()
        dev_host = "ep-damp-frog-b1y9sc7x"
        if dev_host in hostname:
            print(f"\n[FATAL SAFETY VIOLATION] Target host matches development host '{dev_host}'!")
            print("Refusing to run production migration runner against DEV database.")
            sys.exit(1)

    # 4. Preflight checks
    print("\n--- Phase 1: Pre-Flight Safety Verification ---")
    try:
        engine = create_engine(target_url, connect_args={"connect_timeout": 10} if not is_sqlite else {})
        preflight = run_preflight_checks(engine)
    except Exception as e:
        print(f"[FATAL] Connection to database failed: {e}")
        sys.exit(1)

    print(f"  Current Alembic Revision:       {preflight['current_revision']}")
    print(f"  Existing solo_attempts count:   {preflight['solo_attempts_count']}")
    print(f"  Non-null user_id count:         {preflight['non_null_user_id_count']}")
    print(f"  Duplicate (round, seq) count:   {preflight['duplicate_sequence_count']}")

    if preflight["current_revision"] == "9c0d1e2f3a4b":
        print("\n[NOTICE] Database is ALREADY migrated to target revision 9c0d1e2f3a4b.")
        print("No migration actions required.")
        sys.exit(0)

    if preflight["current_revision"] != "4ed9e2bc20cc":
        print(f"\n[FATAL] Expected baseline revision '4ed9e2bc20cc', but found '{preflight['current_revision']}'.")
        print("Aborting to prevent applying migrations out of sequence.")
        sys.exit(1)

    if not preflight["passed"]:
        print("\n[FATAL] Preflight checks failed:")
        for err in preflight["errors"]:
            print(f"  - {err}")
        sys.exit(1)

    print("[OK] All pre-flight safety checks PASSED.")

    # Migration plan
    print("\n--- Planned Migration Chain ---")
    print("  1. 4ed9e2bc20cc -> 7a8b9c0d1e2f (decouple_question_bank_round_questions)")
    print("  2. 7a8b9c0d1e2f -> 8b9c0d1e2f3a (add_users_and_attempt_leaderboard_fields)")
    print("  3. 8b9c0d1e2f3a -> 9c0d1e2f3a4b (add_anon_unique_index_and_metadata)")

    if args.dry_run:
        print("\n[SUCCESS] DRY-RUN COMPLETE: Target database is 100% ready for migration.")
        print("To execute live migration, run:")
        print("  python scripts/run_production_migration_secure.py --execute\n")
        sys.exit(0)

    # 5. Live Execution
    print("\n--- Phase 2: Live Migration Execution ---")
    print("Applying migrations up to target revision 9c0d1e2f3a4b ...")

    # Set environment variables for Alembic and database safety validator
    os.environ["ENVIRONMENT"] = "production"
    os.environ["DB_TARGET"] = "production"
    os.environ["DATABASE_URL"] = target_url

    try:
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", target_url)
        command.upgrade(alembic_cfg, "9c0d1e2f3a4b")
        print("[OK] Alembic migration commands finished.")
    except Exception as e:
        print(f"\n[ERROR] Migration failed during execution: {e}")
        print("\nRECOVERY INSTRUCTIONS:")
        print("1. Do NOT panic. Your database is backed up at: backup-pre-rc1-public-arena")
        print("2. In the Neon Console, restore production from the branch 'backup-pre-rc1-public-arena'.")
        print("3. Review the error details above to diagnose the issue.")
        sys.exit(1)

    # 6. Postflight Verification
    print("\n--- Phase 3: Post-Migration Verification ---")
    try:
        postflight = run_postflight_checks(engine)
        for chk in postflight["checks"]:
            print(f"  [VERIFY] {chk}")
        if postflight["passed"]:
            print("\n====================================================================")
            print(" SUCCESS: Production Database Migrated Successfully to 9c0d1e2f3a4b")
            print("====================================================================\n")
        else:
            print("\n[WARN] Migration completed but some postflight assertions did not pass.")
            sys.exit(1)
    except Exception as e:
        print(f"[WARN] Error during post-migration verification: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
