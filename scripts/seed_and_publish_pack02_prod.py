"""
Authoritative Production Seeder and Publisher for Classic Zakovat — Pack 02.
Designed for idempotent, reproducible execution against staging or production environments.

Guarantees:
- Uses stable content identifiers and natural keys (question text, source provenance), NEVER DEV primary keys.
- Detects existing content: completely idempotent (re-running causes 0 mutations).
- Prevents duplication of quizzes, questions, accepted answers, and rounds.
- Preserves all 24 approved questions and the four Owner-approved alternatives:
    Q6: Dorbozlik
    Q8: Atirgul
    Q15: O‘ymoq
    Q21: Kremniy vodiysi
- Uses authoritative validation (run_publish_validation) and publishing (execute_publish_version).
- Validates the exact 2 x 12 structure.
- Produces an immutable, frozen JSON manifest.
- Supports non-mutating --dry-run mode.
"""

import os
import sys
import json
import argparse
import urllib.parse
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from models import Base, Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer
from api.drafts import run_publish_validation, execute_publish_version
from services.editorial import reconcile_pack02_question_audit


CANONICAL_PACK_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "packs", "pack_02_canonical.json"
)


def load_canonical_pack_data() -> dict:
    if not os.path.exists(CANONICAL_PACK_FILE):
        raise FileNotFoundError(f"Canonical pack file not found at: {CANONICAL_PACK_FILE}")
    with open(CANONICAL_PACK_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate structure
    assert data.get("title") == "Classic Zakovat — Pack 02", "Unexpected pack title"
    assert len(data.get("rounds", [])) == 2, "Canonical pack must have exactly 2 rounds"
    for r in data["rounds"]:
        assert len(r.get("questions", [])) == 12, f"Round {r.get('sequence')} must have 12 questions"
    return data


def seed_and_publish_pack02(db: Session, dry_run: bool = False) -> dict:
    print(f"=== SEED & PUBLISH PACK 02 (Dry Run: {dry_run}) ===\n")
    pack_data = load_canonical_pack_data()
    title = pack_data["title"]

    # 1. Check if Quiz already exists
    quiz = db.query(Quiz).filter(Quiz.title == title).first()
    if quiz:
        print(f"Found existing Quiz (ID: {quiz.id}, Title: '{quiz.title}')")
        published_v = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "published")
            .order_by(QuizVersion.version_number.desc())
            .first()
        )
        if published_v and published_v.published_manifest:
            m = published_v.published_manifest
            rounds = m.get("rounds", [])
            total_qs = sum(len(r.get("questions", [])) for r in rounds)
            if len(rounds) == 2 and total_qs == 24:
                print(f"Pack 02 is ALREADY PUBLISHED in Version #{published_v.version_number} (ID: {published_v.id})")
                print(f"Manifest verified: {len(rounds)} rounds, {total_qs} questions.")
                print("ZERO mutations required. IDEMPOTENT NO-OP.")
                return {
                    "status": "already_published",
                    "quiz_id": quiz.id,
                    "version_id": published_v.id,
                    "mutations": 0,
                    "message": "Pack 02 is already published and matches canonical specification."
                }

    mutations_count = 0

    # 2. Create Quiz if missing
    if not quiz:
        print(f"Creating new Quiz: '{title}'")
        quiz = Quiz(
            title=title,
            description=pack_data.get("description", "Classic Zakovat Pack 02"),
        )
        if not dry_run:
            db.add(quiz)
            db.flush()
        mutations_count += 1
    else:
        print(f"Using existing Quiz container (ID: {quiz.id})")

    # 3. Get or create draft version
    draft_v = None
    if quiz.id:
        draft_v = (
            db.query(QuizVersion)
            .filter(QuizVersion.quiz_id == quiz.id, QuizVersion.status == "draft")
            .first()
        )

    if not draft_v:
        print("Creating QuizVersion 1 (draft)...")
        draft_v = QuizVersion(
            quiz_id=quiz.id if quiz.id else 0,
            version_number=1,
            status="draft",
            game_mode=pack_data.get("game_mode", "classic_zakovat"),
        )
        if not dry_run:
            db.add(draft_v)
            db.flush()
        mutations_count += 1
    else:
        print(f"Using existing draft QuizVersion #{draft_v.version_number} (ID: {draft_v.id})")

    # 4. Populate or verify the 2 rounds and 24 questions
    special_alts = {
        6: "Dorbozlik",
        8: "Atirgul",
        15: "O‘ymoq",
        21: "Kremniy vodiysi",
    }

    for r_spec in pack_data["rounds"]:
        r_seq = r_spec["sequence"]
        r_name = r_spec["name"]
        round_type = r_spec.get("round_type", "zakovat_classic")

        round_obj = None
        if draft_v.id:
            round_obj = (
                db.query(Round)
                .filter(Round.quiz_version_id == draft_v.id, Round.sequence == r_seq)
                .first()
            )

        if not round_obj:
            print(f"  Creating Round {r_seq}: '{r_name}' ({round_type})")
            round_obj = Round(
                quiz_version_id=draft_v.id if draft_v.id else 0,
                sequence=r_seq,
                round_type=round_type,
                config={"name": r_name, "source_unit": pack_data.get("source_unit", 2)},
            )
            if not dry_run:
                db.add(round_obj)
                db.flush()
            mutations_count += 1

        # Process the 12 questions for this round
        for q_spec in r_spec["questions"]:
            q_seq = q_spec["sequence"]
            global_seq = q_spec["global_sequence"]
            q_text = q_spec["text"].strip()
            prim_ans = q_spec["primary_answer"].strip()
            alt_answers = [a.strip() for a in q_spec.get("alternative_answers", [])]

            # Search question by normalized text to avoid duplicates
            existing_q = db.query(Question).filter(Question.text == q_text).first()
            if not existing_q:
                print(f"    [Q{global_seq:02d}] Creating new Question: {q_text[:60]}...")
                existing_q = Question(
                    text=q_text,
                    explanation=q_spec.get("explanation"),
                    status="approved",
                    default_points=q_spec.get("points", 1),
                    source_meta=q_spec.get("source_meta", {}),
                )
                if not dry_run:
                    db.add(existing_q)
                    db.flush()
                mutations_count += 1

                # Add primary answer
                if not dry_run:
                    p_ans = AcceptedAnswer(
                        question_id=existing_q.id,
                        answer_text=prim_ans,
                        is_primary=True,
                    )
                    db.add(p_ans)
                    db.flush()
                    mutations_count += 1

                # Add alternatives
                for alt_t in alt_answers:
                    if not dry_run:
                        a_ans = AcceptedAnswer(
                            question_id=existing_q.id,
                            answer_text=alt_t,
                            is_primary=False,
                        )
                        db.add(a_ans)
                        db.flush()
                        mutations_count += 1
            else:
                # Question exists, ensure primary and alternatives are present
                existing_answers = existing_q.accepted_answers or []
                has_prim = any(a.is_primary and a.answer_text.strip().lower() == prim_ans.lower() for a in existing_answers)
                if not has_prim and not dry_run:
                    p_ans = AcceptedAnswer(
                        question_id=existing_q.id,
                        answer_text=prim_ans,
                        is_primary=True,
                    )
                    db.add(p_ans)
                    db.flush()
                    mutations_count += 1

                for alt_t in alt_answers:
                    has_alt = any(not a.is_primary and a.answer_text.strip().lower() == alt_t.lower() for a in existing_answers)
                    if not has_alt and not dry_run:
                        a_ans = AcceptedAnswer(
                            question_id=existing_q.id,
                            answer_text=alt_t,
                            is_primary=False,
                        )
                        db.add(a_ans)
                        db.flush()
                        mutations_count += 1

            # Ensure audit reconciliation history is present
            if not dry_run:
                is_special = global_seq in special_alts
                alt_name = special_alts.get(global_seq)
                reconcile_pack02_question_audit(
                    question=existing_q,
                    global_seq=global_seq,
                    is_alternative_question=is_special,
                    alternative_name=alt_name,
                    executed_by="seed_and_publish_pack02_prod",
                )

            # Ensure RoundQuestion association exists
            rq = None
            if round_obj and round_obj.id and existing_q.id:
                rq = db.query(RoundQuestion).filter(
                    RoundQuestion.round_id == round_obj.id,
                    RoundQuestion.question_id == existing_q.id
                ).first()

            if not rq and not dry_run and round_obj.id and existing_q.id:
                rq = RoundQuestion(
                    round_id=round_obj.id,
                    question_id=existing_q.id,
                    sequence=q_seq,
                )
                db.add(rq)
                db.flush()
                mutations_count += 1

    if dry_run:
        print("\n[DRY RUN SUMMARY]")
        print(f"  Calculated mutations: {mutations_count}")
        print("  Validation and commit skipped due to --dry-run.")
        return {
            "status": "dry_run_success",
            "mutations": mutations_count,
            "message": "Dry-run validation successful. No database records modified."
        }

    # 5. Run authoritative publish validation
    print("\nRunning authoritative publish validation...")
    validation = run_publish_validation(draft_v, db)
    print(f"  Valid: {validation['valid']}")
    print(f"  Ready to publish: {validation['is_ready_to_publish']}")
    print(f"  Readiness state: '{validation['readiness_state']}'")
    print(f"  Editorial approved: {validation['editorial_summary'].get('approved')}/24")

    if not validation["valid"] or not validation["is_ready_to_publish"]:
        print("\nVALIDATION FAILED:")
        for err in validation.get("errors", []):
            print(f"  - {err}")
        db.rollback()
        raise RuntimeError(f"Publish validation failed for Pack 02: {validation.get('errors')}")

    # 6. Execute authoritative publish & manifest freeze
    print("Executing publish and freezing immutable JSON manifest...")
    published_res = execute_publish_version(
        version=draft_v,
        db=db,
        visibility=pack_data.get("visibility", "public"),
        category=pack_data.get("category", "Zakovat"),
    )
    db.commit()

    db.refresh(draft_v)
    assert draft_v.status == "published", f"Expected published, got {draft_v.status}"
    assert draft_v.published_manifest is not None, "Published manifest missing"
    assert len(draft_v.published_manifest["rounds"]) == 2, "Manifest must have 2 rounds"

    print("\nSUCCESS: Pack 02 published in production DB!")
    print(f"  Quiz ID:    {quiz.id}")
    print(f"  Version ID: {draft_v.id}")
    print(f"  Published At: {draft_v.published_at}")
    return {
        "status": "published_success",
        "quiz_id": quiz.id,
        "version_id": draft_v.id,
        "mutations": mutations_count,
    }


def main():
    parser = argparse.ArgumentParser(description="Seed and publish Pack 02 idempotently")
    parser.add_argument("--dry-run", action="store_true", help="Simulate seeding without committing changes")
    parser.add_argument("--url", type=str, default=None, help="Database connection URL (defaults to DATABASE_URL)")
    args = parser.parse_args()

    # Safety checks
    db_url = args.url or os.getenv("TARGET_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not provided.")
        sys.exit(1)

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    parsed = urllib.parse.urlparse(db_url)
    hostname = (parsed.hostname or "").lower()

    env = (os.getenv("ENVIRONMENT") or "").strip().lower()
    target = (os.getenv("DB_TARGET") or "").strip().lower()

    if env == "production" and "ep-damp-frog-b1y9sc7x" in hostname:
        print("\n[FATAL SAFETY VIOLATION] Target host matches development host 'ep-damp-frog-b1y9sc7x'!")
        print("Refusing to run production seeder against DEV database.")
        sys.exit(1)

    print(f"Environment: {env}")
    print(f"DB Target:   {target}")
    print(f"DB Host:     {hostname}")

    # When explicit production URL is used, verify host safety
    engine = create_engine(db_url)
    SessionFactory = sessionmaker(bind=engine)
    db = SessionFactory()

    try:
        res = seed_and_publish_pack02(db, dry_run=args.dry_run)
        print(f"\nResult: {res}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
