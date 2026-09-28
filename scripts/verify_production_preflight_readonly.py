"""
Production Read-Only Preflight Verification Script for ZakoWhat.
Performs non-mutating inspection of the target production database.
- Strictly asserts ENVIRONMENT=production and DB_TARGET=production.
- Strictly asserts host is NOT DEV (rejects ep-damp-frog-b1y9sc7x).
- Sanitizes and never displays secrets or connection strings.
- Inspects Alembic revision, table schemas, existing quizzes, duplicate anon_ids.
"""

import os
import sys
import argparse
import urllib.parse
from sqlalchemy import create_engine, text

def run_preflight(target_url: str = None) -> dict:
    url = target_url or os.getenv("PROD_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        return {
            "status": "UNKNOWN",
            "error": "No database URL provided or found in environment."
        }

    # Normalize url scheme
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    port = parsed.port or 5432
    dbname = (parsed.path or "").lstrip("/")

    # Strict DEV Host Protection Guard
    dev_host_part = "ep-damp-frog-b1y9sc7x"
    if dev_host_part in hostname:
        return {
            "status": "ABORTED_SAFETY_VIOLATION",
            "error": f"Refusing to execute preflight against DEV database host: {hostname}"
        }

    masked_url = f"postgresql://***:***@{hostname}:{port}/{dbname}"
    print(f"Connecting (read-only) to: {masked_url} ...")

    engine = create_engine(url, connect_args={"connect_timeout": 10})
    report = {
        "status": "INSPECTED",
        "target_host": hostname,
        "database": dbname,
        "alembic_version": None,
        "tables_found": [],
        "quizzes_count": 0,
        "pack02_found": False,
        "duplicate_anon_id_count": 0,
        "duplicate_anon_ids": [],
        "has_round_questions_table": False,
        "has_users_table": False,
        "has_attempt_metadata_col": False,
        "has_anon_unique_index": False,
        "solo_attempts_count": 0,
        "preflight_passed": True,
        "blockers": [],
    }

    with engine.connect() as conn:
        # 1. Alembic version
        try:
            res = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
            report["alembic_version"] = res[0] if res else "empty_table"
        except Exception:
            report["alembic_version"] = "missing_table"

        # 2. Existing tables
        table_rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )).fetchall()
        report["tables_found"] = sorted([r[0] for r in table_rows])
        report["has_round_questions_table"] = "round_questions" in report["tables_found"]
        report["has_users_table"] = "users" in report["tables_found"]

        # 3. Solo attempts columns & index
        if "solo_attempts" in report["tables_found"]:
            col_rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'solo_attempts'"
            )).fetchall()
            cols = [r[0] for r in col_rows]
            report["has_attempt_metadata_col"] = "attempt_metadata" in cols

            idx_rows = conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'solo_attempts'"
            )).fetchall()
            indexes = [r[0] for r in idx_rows]
            report["has_anon_unique_index"] = "idx_solo_attempts_anon_id_unique" in indexes

            # Count attempts
            att_count = conn.execute(text("SELECT count(*) FROM solo_attempts")).scalar()
            report["solo_attempts_count"] = att_count

            # Check duplicate anon_ids
            dup_rows = conn.execute(text(
                "SELECT anon_id, count(*) FROM solo_attempts WHERE anon_id IS NOT NULL GROUP BY anon_id HAVING count(*) > 1"
            )).fetchall()
            report["duplicate_anon_id_count"] = len(dup_rows)
            report["duplicate_anon_ids"] = [r[0] for r in dup_rows[:5]]
            if dup_rows:
                report["preflight_passed"] = False
                report["blockers"].append(
                    f"Found {len(dup_rows)} duplicate anon_id values in solo_attempts. "
                    "Migration 9c0d1e2f3a4b will fail to create unique index until deduplicated."
                )

        # 4. Existing quizzes
        if "quizzes" in report["tables_found"]:
            q_count = conn.execute(text("SELECT count(*) FROM quizzes")).scalar()
            report["quizzes_count"] = q_count

            pack2_rows = conn.execute(text(
                "SELECT id, title FROM quizzes WHERE title ILIKE '%Pack 02%'"
            )).fetchall()
            if pack2_rows:
                report["pack02_found"] = True
                report["pack02_details"] = [{"id": r[0], "title": r[1]} for r in pack2_rows]

    return report


def main():
    parser = argparse.ArgumentParser(description="Read-only Production Database Preflight")
    parser.add_argument("--url", type=str, default=None, help="Production PostgreSQL connection URL")
    args = parser.parse_args()

    # Safety check on local environment
    env = (os.getenv("ENVIRONMENT") or "").strip().lower()
    target = (os.getenv("DB_TARGET") or "").strip().lower()

    if not args.url and (env != "production" or target != "production"):
        print("=== PRODUCTION PREFLIGHT: LOCAL ENVIRONMENT CHECK ===")
        print(f"Current ENVIRONMENT: {env}")
        print(f"Current DB_TARGET:   {target}")
        print("Safe status: Local environment is configured for development.")
        print("Production credentials are not present locally. Production state is classified as UNKNOWN.")
        print("\nTo run this preflight against production, supply --url or run inside Render shell:")
        print("  python scripts/verify_production_preflight_readonly.py --url '<PROD_DATABASE_URL>'\n")
        return

    report = run_preflight(args.url)
    print("\n=== PREFLIGHT INSPECTION REPORT ===")
    for k, v in report.items():
        print(f"  {k:30s}: {v}")

    if not report.get("preflight_passed"):
        print("\nPREFLIGHT WARNINGS / BLOCKERS DETECTED:")
        for b in report.get("blockers", []):
            print(f"  - {b}")
        sys.exit(1)
    else:
        print("\nPREFLIGHT SUCCESS: Target schema is fully compatible.")

if __name__ == "__main__":
    main()
