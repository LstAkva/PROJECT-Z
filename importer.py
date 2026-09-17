import argparse
import json
import os
from collections import Counter

from database import SessionLocal
from models import Question, AcceptedAnswer
from quality_gate import evaluate_record
from services.gameplay import normalize_uzbek_latin


class QuestionValidator:
    def __init__(self):
        self.stats = {
            "total_processed": 0,
            "eligible": 0,
            "rejected": 0,
            "needs_review": 0,
        }
        self.rejected_records = []

    def _reject(self, filename, index, record, reason):
        self.stats["rejected"] += 1

        raw_text = record.get("text", "") or ""
        text_preview = str(raw_text)[:80].replace("\n", " ") + "..."

        self.rejected_records.append({
            "filename": filename,
            "index": index,
            "text_preview": text_preview,
            "reason": reason,
        })

    def validate_and_normalize(self, record, filename, index):
        self.stats["total_processed"] += 1

        # =========================================================
        # 1. PROVENANCE
        # =========================================================
        source_data = record.get("source", {})

        if not isinstance(source_data, dict):
            source_data = {}

        channel_id = source_data.get("channel_id")
        question_message_id = source_data.get("question_message_id")

        source_file = source_data.get("source_file")
        question_number = source_data.get("question_number")

        has_tg_prov = bool(channel_id and question_message_id)
        has_file_prov = bool(source_file and question_number)

        if not (has_tg_prov or has_file_prov):
            self._reject(
                filename,
                index,
                record,
                "missing_provenance",
            )
            return None

        # =========================================================
        # 2. QUESTION TEXT
        # =========================================================
        question_text = record.get("text")

        if not question_text or not str(question_text).strip():
            self._reject(
                filename,
                index,
                record,
                "missing_question_text",
            )
            return None

        question_text = str(question_text).strip()

        # =========================================================
        # 3. PRIMARY ANSWER
        # =========================================================
        primary_answer = record.get("primary_answer")

        if not primary_answer or not str(primary_answer).strip():
            self._reject(
                filename,
                index,
                record,
                "missing_primary_answer",
            )
            return None

        primary_answer = str(primary_answer).strip()

        # =========================================================
        # 4. EDITORIAL
        # =========================================================
        editorial_data = record.get("editorial", {})

        if not isinstance(editorial_data, dict):
            editorial_data = {}

        flags = editorial_data.get("flags", [])

        if not isinstance(flags, list):
            flags = []

        needs_review = False

        if "missing_media" in flags:
            needs_review = True

        # =========================================================
        # 5. MEDIA
        # =========================================================
        media_field = record.get("media")

        if isinstance(media_field, dict):
            if "media_dependent" in flags and not media_field.get("url"):
                needs_review = True
        elif media_field is not None:
            media_field = None

        # =========================================================
        # 6. POINTS
        # =========================================================
        points = record.get("points")

        if points is not None:
            try:
                points = int(points)
            except (ValueError, TypeError):
                points = 1
        else:
            points = 1

        # =========================================================
        # 7. METRICS
        # =========================================================
        if needs_review:
            self.stats["needs_review"] += 1
        else:
            self.stats["eligible"] += 1

        return {
            "id": record.get("id"),
            "text": question_text,
            "primary_answer": primary_answer,
            "accepted_answers": record.get("accepted_answers", []),
            "explanation": record.get("explanation"),
            "category": record.get("category"),
            "source": source_data,
            "editorial": editorial_data,
            "compound": record.get("compound"),
            "points": points,
            "media": media_field,
            "needs_review": needs_review,
            "round_type": record.get("round_type"),
            "options": record.get("options"),
            "question_type": record.get("question_type", "text"),
        }


def print_validation_report(filename, validator):
    print(f"\n=== VALIDATION REPORT [{filename}] ===")
    print(f"Total Processed: {validator.stats['total_processed']}")
    print(f"Eligible        : {validator.stats['eligible']}")
    print(f"Rejected        : {validator.stats['rejected']}")
    print(f"Needs Review    : {validator.stats['needs_review']}")

    if validator.rejected_records:
        error_counts = Counter(
            item["reason"]
            for item in validator.rejected_records
        )

        print("\n--- ERRORS GROUPED BY TYPE ---")

        for reason, count in error_counts.most_common():
            print(f"{reason}: {count}")

        print("\n--- REJECTED RECORDS PREVIEW (Top 10) ---")

        for item in validator.rejected_records[:10]:
            print(
                f"[Index: {item['index']}] "
                f"REASON: {item['reason']}"
            )
            print(
                f"Preview: {item['text_preview']}\n"
            )


def validate_only(json_filepath):
    filename = os.path.basename(json_filepath)

    with open(json_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(
            "Canonical JSON must contain a top-level array."
        )

    validator = QuestionValidator()

    for index, record in enumerate(data):
        validator.validate_and_normalize(
            record,
            filename,
            index,
        )

    print("\n=== VALIDATE ONLY ===")
    print_validation_report(filename, validator)
    print("\nNo database changes were made.")


# =========================================================
# DEDUPLICATION LOGIC
# =========================================================

def find_existing_canonical_id(db, canonical_id):
    if not canonical_id:
        return None
    try:
        res = (
            db.query(Question)
            .filter(Question.source_meta["canonical_id"].astext == str(canonical_id))
            .first()
        )
        if res:
            return res
    except Exception:
        pass

    # Dialect-neutral fallback
    for candidate in db.query(Question).all():
        if candidate.source_meta and str(candidate.source_meta.get("canonical_id")) == str(canonical_id):
            return candidate
    return None


def find_existing_telegram_question(
    db,
    channel_id,
    question_message_id,
    points,
    question_text,
    primary_answer,
):
    """
    Telegram deduplication across messages.
    """
    candidates = []
    try:
        candidates = (
            db.query(Question)
            .filter(
                Question.source_meta["channel_id"].astext == str(channel_id),
                Question.source_meta["question_message_id"].astext == str(question_message_id),
            )
            .all()
        )
    except Exception:
        for q in db.query(Question).all():
            sm = q.source_meta or {}
            if str(sm.get("channel_id")) == str(channel_id) and str(sm.get("question_message_id")) == str(question_message_id):
                candidates.append(q)

    for candidate in candidates:
        stored_points = candidate.source_meta.get("points") if candidate.source_meta else None
        stored_text = candidate.source_meta.get("question_text") if candidate.source_meta else None
        stored_primary = candidate.source_meta.get("primary_answer") if candidate.source_meta else None

        points_match = (stored_points == points) if stored_points is not None else (candidate.points == points)
        text_match = (stored_text == question_text) if stored_text is not None else (candidate.text == question_text)

        if stored_primary is not None:
            primary_match = stored_primary == primary_answer
        else:
            primary_record = (
                db.query(AcceptedAnswer)
                .filter(
                    AcceptedAnswer.question_id == candidate.id,
                    AcceptedAnswer.is_primary.is_(True),
                )
                .first()
            )
            primary_match = (
                primary_record is not None
                and primary_record.answer_text == primary_answer
            )

        if points_match and text_match and primary_match:
            return candidate

    return None


def find_existing_file_question(
    db,
    source_file,
    question_number,
):
    try:
        res = (
            db.query(Question)
            .filter(
                Question.source_meta["source_file"].astext == str(source_file),
                Question.source_meta["question_number"].astext == str(question_number),
            )
            .first()
        )
        if res:
            return res
    except Exception:
        pass

    for q in db.query(Question).all():
        sm = q.source_meta or {}
        if str(sm.get("source_file")) == str(source_file) and str(sm.get("question_number")) == str(question_number):
            return q
    return None


def find_existing_content_match(db, question_text, primary_answer):
    """
    Global Question Bank deduplication: checks if an identical question (normalized text + primary answer)
    already exists in the bank, regardless of source channel, file, or apostrophe typographical variance.
    """
    if not question_text or not primary_answer:
        return None

    norm_text = normalize_uzbek_latin(question_text)
    norm_ans = normalize_uzbek_latin(primary_answer)

    # 1. First check exact text match
    candidates = db.query(Question).filter(Question.text == question_text).all()
    for cand in candidates:
        for ans in cand.accepted_answers:
            if ans.is_primary and normalize_uzbek_latin(ans.answer_text) == norm_ans:
                return cand

    # 2. Check by primary answer (fast and handles apostrophe variants in question text)
    ans_candidates = (
        db.query(AcceptedAnswer)
        .filter(AcceptedAnswer.is_primary.is_(True))
        .all()
    )
    for ans in ans_candidates:
        if normalize_uzbek_latin(ans.answer_text) == norm_ans:
            q = ans.question
            if q and normalize_uzbek_latin(q.text) == norm_text:
                return q

    return None


# =========================================================
# QUESTION BANK INGESTION
# =========================================================

def import_questions(json_filepath, dry_run=False, limit=None, allow_needs_review=False, db_session=None):
    """
    Imports canonical questions directly into the central Question Bank.
    Does NOT create any fake Quiz, fake QuizVersion, or fake import_buffer Round.
    """
    filename = os.path.basename(json_filepath)

    owns_db = db_session is None
    db = db_session if db_session is not None else SessionLocal()
    validator = QuestionValidator()

    stats = {
        "total": 0,
        "added": 0,
        "skipped_duplicate": 0,
        "skipped_rejected": 0,
        "skipped_needs_review": 0,
        "needs_review": 0,
        "ready": 0,
        "draft": 0,
        "compound": 0,
        "jeopardy": 0,
        "media_dependent": 0,
    }

    try:
        # 1. LOAD CANONICAL JSON
        with open(json_filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError("Canonical JSON must contain a top-level array.")

        if limit is not None and limit > 0:
            data = data[:limit]
            print(f"Limiting to first {limit} records.")

        stats["total"] = len(data)

        print(f"Importing directly into Question Bank: {filename} {'[DRY RUN]' if dry_run else ''}")
        print(f"Canonical records to process: {len(data)}")

        # 2. PROCESS RECORDS
        for index, raw_item in enumerate(data):
            # Quality gate evaluation
            qg_status, qg_reasons = evaluate_record(raw_item, index)
            if qg_status == "rejected":
                stats["skipped_rejected"] += 1
                continue
            if qg_status == "needs_review" and not allow_needs_review:
                stats["skipped_needs_review"] += 1
                continue

            item = validator.validate_and_normalize(raw_item, filename, index)
            if item is None:
                stats["skipped_rejected"] += 1
                continue

            source = item["source"]
            channel_id = source.get("channel_id")
            question_message_id = source.get("question_message_id")
            source_file = source.get("source_file")
            question_number = source.get("question_number")

            points = item.get("points", 1) or 1
            question_text = item["text"]
            primary_answer = item["primary_answer"]

            # Multi-layer deduplication
            existing_q = None

            # 2a. Canonical ID
            canonical_id = item.get("id") or raw_item.get("id")
            if canonical_id:
                existing_q = find_existing_canonical_id(db, canonical_id)

            # 2b. Telegram / File Provenance
            if existing_q is None:
                if channel_id and question_message_id:
                    existing_q = find_existing_telegram_question(
                        db=db,
                        channel_id=channel_id,
                        question_message_id=question_message_id,
                        points=points,
                        question_text=question_text,
                        primary_answer=primary_answer,
                    )
                elif source_file and question_number:
                    existing_q = find_existing_file_question(
                        db=db,
                        source_file=source_file,
                        question_number=question_number,
                    )

            # 2c. Global Content Match (Normalized text + primary answer)
            if existing_q is None:
                existing_q = find_existing_content_match(db, question_text, primary_answer)

            if existing_q is not None:
                stats["skipped_duplicate"] += 1
                continue

            # Status resolution:
            # Quality Gate decision is authoritative for whether a record is admitted and its Question Bank status.
            # Raw canonical editorial.status (e.g. 'needs_review' on raw items) must NOT override Quality Gate.
            if qg_status == "ready_to_import":
                final_status = "ready"
            elif qg_status == "needs_review":
                final_status = "needs_review"
            else:
                final_status = "draft"

            stats[final_status] += 1

            # Compound
            compound_data = item.get("compound")
            if isinstance(compound_data, dict):
                stats["compound"] += 1
                compound_group_id = compound_data.get("group_id")
                compound_type = compound_data.get("type")
            else:
                compound_group_id = None
                compound_type = None

            if item.get("round_type") == "jeopardy":
                stats["jeopardy"] += 1

            editorial = item.get("editorial", {}) or {}
            flags = editorial.get("flags", [])
            if "media_dependent" in flags or "missing_media" in flags:
                stats["media_dependent"] += 1

            # Source Meta
            source_meta = {
                "source_name": source.get("source_name"),
                "channel_name": source.get("channel_name"),
                "channel_id": channel_id,
                "question_message_id": question_message_id,
                "source_file": source_file,
                "question_number": question_number,
                "source_page": source.get("source_page"),
                "pack": source.get("pack"),
                "canonical_id": canonical_id,
                "question_text": question_text,
                "primary_answer": primary_answer,
                "points": points,
            }

            # Media resolution
            media_url = None
            media_provider = None
            media_field = item.get("media")
            if isinstance(media_field, dict):
                media_url = media_field.get("url")
                media_provider = media_field.get("provider")

            # MCQ options resolution
            options = item.get("options")
            q_type = item.get("question_type", "text")
            if options and isinstance(options, list) and q_type == "text":
                q_type = "mcq"

            # Create standalone Question in Question Bank
            new_question = Question(
                round_id=None,
                sequence=None,
                text=question_text,
                explanation=item.get("explanation"),
                status=final_status,
                source_meta=source_meta,
                points=points,
                default_points=points,
                category=item.get("category"),
                compound_group_id=compound_group_id,
                compound_type=compound_type,
                options=options,
                question_type=q_type,
                media_provider=media_provider,
                media_url=media_url,
            )
            db.add(new_question)
            db.flush()

            # Primary answer
            db.add(
                AcceptedAnswer(
                    question_id=new_question.id,
                    answer_text=primary_answer,
                    is_primary=True,
                )
            )

            # Alternate accepted answers
            seen_alternatives = set()
            for answer in item.get("accepted_answers", []):
                if not isinstance(answer, dict):
                    continue
                ans_text = answer.get("text")
                ans_type = answer.get("type")

                if not ans_text:
                    continue
                ans_text = str(ans_text).strip()
                if not ans_text or ans_type == "primary" or ans_text == primary_answer:
                    continue
                if ans_text in seen_alternatives:
                    continue

                seen_alternatives.add(ans_text)
                db.add(
                    AcceptedAnswer(
                        question_id=new_question.id,
                        answer_text=ans_text,
                        is_primary=False,
                    )
                )

            stats["added"] += 1

        if dry_run:
            db.rollback()
            print("\n[DRY RUN] Database transaction rolled back. No rows were committed.")
        else:
            db.commit()

        print_validation_report(filename, validator)
        print("\n=== QUESTION BANK IMPORT SUMMARY ===")
        print(f"Mode               : {'DRY RUN (Read-Only)' if dry_run else 'LIVE COMMIT'}")
        print(f"Total processed    : {stats['total']}")
        print(f"{'Would Add' if dry_run else 'Added'}          : {stats['added']}")
        print(f"  - Ready          : {stats['ready']}")
        print(f"  - Needs Review   : {stats['needs_review']}")
        print(f"  - Draft          : {stats['draft']}")
        print(f"Skipped (duplicate): {stats['skipped_duplicate']}")
        print(f"Skipped (rejected) : {stats['skipped_rejected']}")
        print(f"Skipped (needs rev): {stats['skipped_needs_review']}")
        print("===================================\n")

    except Exception as exc:
        db.rollback()
        print("\n❌ Import failed. Transaction rolled back.")
        print(f"Error: {exc}\n")
        raise
    finally:
        if owns_db:
            db.close()

    return stats


def parse_args():
    parser = argparse.ArgumentParser(description="ZakoWhat canonical question bank importer")
    parser.add_argument("--validate-only", action="store_true", help="Validate canonical JSON without touching DB")
    parser.add_argument("--dry-run", action="store_true", help="Simulate import with rollback")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of records to process")
    parser.add_argument("--allow-needs-review", action="store_true", help="Also import questions flagged as needs_review")
    parser.add_argument("json_file", nargs="?", default="canonical_1.json", help="Canonical JSON file to process")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.validate_only:
        validate_only(args.json_file)
    else:
        import_questions(
            args.json_file,
            dry_run=args.dry_run,
            limit=args.limit,
            allow_needs_review=args.allow_needs_review,
        )