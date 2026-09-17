import argparse
import json
import os
from collections import Counter

from database import SessionLocal
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer


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

        if editorial_data.get("status") == "needs_review":
            needs_review = True

        if "missing_media" in flags:
            needs_review = True

        # =========================================================
        # 5. MEDIA
        # =========================================================
        media_field = record.get("media")

        if isinstance(media_field, list):
            media_field = ", ".join(str(m) for m in media_field)
            needs_review = True
        elif media_field is None:
            media_field = None
        elif not isinstance(media_field, str):
            media_field = str(media_field)

        if record.get("missing_media"):
            needs_review = True

        if needs_review:
            self.stats["needs_review"] += 1

        # =========================================================
        # 6. ACCEPTED ANSWERS
        #
        # Canonical format:
        #
        # [
        #   {"text": "...", "type": "primary"},
        #   {"text": "...", "type": "zachot"}
        # ]
        # =========================================================
        accepted_answers = record.get("accepted_answers", [])

        if not isinstance(accepted_answers, list):
            accepted_answers = []

        normalized_accepted_answers = []

        for answer in accepted_answers:
            if not isinstance(answer, dict):
                continue

            answer_text = answer.get("text")
            answer_type = answer.get("type")

            if not answer_text or not str(answer_text).strip():
                continue

            normalized_accepted_answers.append({
                "text": str(answer_text).strip(),
                "type": answer_type,
            })

        # =========================================================
        # 7. ELIGIBLE
        # =========================================================
        self.stats["eligible"] += 1

        # =========================================================
        # 8. NORMALIZED RECORD
        # =========================================================
        return {
            "id": record.get("id"),
            "text": question_text,
            "primary_answer": primary_answer,

            "accepted_answers": normalized_accepted_answers,

            "explanation": record.get("explanation"),

            "source": {
                "source_name": source_data.get("source_name"),
                "channel_name": source_data.get("channel_name"),
                "channel_id": channel_id,
                "question_message_id": question_message_id,
                "source_file": source_file,
                "question_number": question_number,
                "source_page": source_data.get("source_page"),
                "pack": source_data.get("pack"),
            },

            "editorial": editorial_data,

            "compound": record.get("compound"),
            "points": record.get("points"),
            "category": record.get("category"),

            "media": media_field,
            "needs_review": needs_review,

            "round_type": record.get("round_type"),
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


def get_or_create_import_round(db):
    """
    Создает технический Quiz / QuizVersion / Round
    для импортированных вопросов, если они еще не существуют.
    """

    quiz = (
        db.query(Quiz)
        .filter_by(title="Telegram Imports Database")
        .first()
    )

    if not quiz:
        quiz = Quiz(
            title="Telegram Imports Database",
            description="База импортированных вопросов из Telegram",
        )
        db.add(quiz)
        db.flush()

    version = (
        db.query(QuizVersion)
        .filter_by(quiz_id=quiz.id)
        .order_by(QuizVersion.version_number.desc())
        .first()
    )

    if not version:
        version = QuizVersion(
            quiz_id=quiz.id,
            version_number=1,
            status="draft",
        )
        db.add(version)
        db.flush()

    import_round = (
        db.query(Round)
        .filter_by(
            quiz_version_id=version.id,
            round_type="import_buffer",
        )
        .first()
    )

    if not import_round:
        import_round = Round(
            quiz_version_id=version.id,
            sequence=1,
            round_type="import_buffer",
        )
        db.add(import_round)
        db.flush()

    return import_round.id


def find_existing_telegram_question(
    db,
    channel_id,
    question_message_id,
    points,
    question_text,
    primary_answer,
):
    """
    Telegram deduplication.

    Один Telegram message может содержать несколько вопросов,
    поэтому channel_id + question_message_id недостаточно.

    Используем:
    channel_id
    + question_message_id
    + points
    + question_text
    + primary_answer
    """

    query = (
        db.query(Question)
        .filter(
            Question.source_meta["channel_id"].astext
            == str(channel_id),

            Question.source_meta["question_message_id"].astext
            == str(question_message_id),
        )
    )

    candidates = query.all()

    for candidate in candidates:
        # Новые импорты хранят эти поля в source_meta.
        stored_points = candidate.source_meta.get("points")
        stored_text = candidate.source_meta.get("question_text")
        stored_primary = candidate.source_meta.get("primary_answer")

        if stored_points is not None:
            points_match = stored_points == points
        else:
            # Fallback для старых записей, импортированных
            # до появления расширенного source_meta.
            points_match = candidate.points == points

        if stored_text is not None:
            text_match = stored_text == question_text
        else:
            text_match = candidate.text == question_text

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
    return (
        db.query(Question)
        .filter(
            Question.source_meta["source_file"].astext
            == str(source_file),

            Question.source_meta["question_number"].astext
            == str(question_number),
        )
        .first()
    )


def import_questions(json_filepath):
    filename = os.path.basename(json_filepath)

    db = SessionLocal()
    validator = QuestionValidator()

    stats = {
        "total": 0,
        "added": 0,
        "skipped_duplicate": 0,
        "skipped_rejected": 0,
        "needs_review": 0,
        "ready": 0,
        "draft": 0,
        "compound": 0,
        "jeopardy": 0,
        "media_dependent": 0,
    }

    try:
        # =========================================================
        # LOAD CANONICAL JSON
        # =========================================================
        with open(json_filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError(
                "Canonical JSON must contain a top-level array."
            )

        stats["total"] = len(data)

        print(f"Importing: {filename}")
        print(f"Canonical records: {len(data)}")

        # =========================================================
        # IMPORT ROUND
        # =========================================================
        round_id = get_or_create_import_round(db)

        max_seq = (
            db.query(Question)
            .filter_by(round_id=round_id)
            .count()
        )

        # =========================================================
        # PROCESS RECORDS
        # =========================================================
        for index, raw_item in enumerate(data):

            # -----------------------------------------------------
            # 1. VALIDATE
            # -----------------------------------------------------
            item = validator.validate_and_normalize(
                raw_item,
                filename,
                index,
            )

            if item is None:
                stats["skipped_rejected"] += 1
                continue

            source = item["source"]

            channel_id = source.get("channel_id")
            question_message_id = source.get(
                "question_message_id"
            )

            source_file = source.get("source_file")
            question_number = source.get(
                "question_number"
            )

            points = item.get("points")
            question_text = item["text"]
            primary_answer = item["primary_answer"]

            # -----------------------------------------------------
            # 2. DEDUPLICATION
            # -----------------------------------------------------
            existing_q = None

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

            else:
                stats["skipped_rejected"] += 1
                validator._reject(
                    filename,
                    index,
                    raw_item,
                    "missing_dedup_provenance",
                )
                continue

            if existing_q is not None:
                stats["skipped_duplicate"] += 1
                continue

            # -----------------------------------------------------
            # 3. FINAL STATUS
            # -----------------------------------------------------
            editorial = item["editorial"]
            canonical_status = editorial.get("status")

            if item["needs_review"]:
                final_status = "needs_review"
            elif canonical_status == "ready":
                final_status = "ready"
            else:
                final_status = "draft"

            stats[final_status] += 1

            # -----------------------------------------------------
            # 4. COMPOUND
            # -----------------------------------------------------
            compound_data = item.get("compound")

            if isinstance(compound_data, dict):
                stats["compound"] += 1

                compound_group_id = compound_data.get(
                    "group_id"
                )
                compound_type = compound_data.get(
                    "type"
                )
            else:
                compound_group_id = None
                compound_type = None

            # -----------------------------------------------------
            # 5. CONTENT TYPE
            # -----------------------------------------------------
            if item.get("round_type") == "jeopardy":
                stats["jeopardy"] += 1

            flags = editorial.get("flags", [])

            if (
                "media_dependent" in flags
                or "missing_media" in flags
            ):
                stats["media_dependent"] += 1

            # -----------------------------------------------------
            # 6. SEQUENCE
            # -----------------------------------------------------
            max_seq += 1

            # -----------------------------------------------------
            # 7. SOURCE META
            # -----------------------------------------------------
            source_meta = {
                "source_name": source.get("source_name"),
                "channel_name": source.get("channel_name"),

                "channel_id": channel_id,
                "question_message_id": question_message_id,

                "source_file": source_file,
                "question_number": question_number,

                "source_page": source.get("source_page"),
                "pack": source.get("pack"),

                # Extended deduplication / traceability
                "canonical_id": item.get("id"),
                "question_text": question_text,
                "primary_answer": primary_answer,
                "points": points,
            }

            # -----------------------------------------------------
            # 8. QUESTION
            # -----------------------------------------------------
            new_question = Question(
                round_id=round_id,
                sequence=max_seq,
                text=question_text,
                explanation=item.get("explanation"),
                status=final_status,
                source_meta=source_meta,
                points=points,
                category=item.get("category"),
                compound_group_id=compound_group_id,
                compound_type=compound_type,
            )

            db.add(new_question)
            db.flush()

            # -----------------------------------------------------
            # 9. PRIMARY ANSWER
            # -----------------------------------------------------
            db.add(
                AcceptedAnswer(
                    question_id=new_question.id,
                    answer_text=primary_answer,
                    is_primary=True,
                )
            )

            # -----------------------------------------------------
            # 10. ACCEPTED / ZACHOT ANSWERS
            # -----------------------------------------------------
            seen_alternatives = set()

            for answer in item.get(
                "accepted_answers",
                [],
            ):

                if not isinstance(answer, dict):
                    continue

                answer_text = answer.get("text")
                answer_type = answer.get("type")

                if not answer_text:
                    continue

                answer_text = str(answer_text).strip()

                if not answer_text:
                    continue

                # Primary answer is already stored separately.
                if answer_type == "primary":
                    continue

                if answer_text == primary_answer:
                    continue

                if answer_text in seen_alternatives:
                    continue

                seen_alternatives.add(answer_text)

                db.add(
                    AcceptedAnswer(
                        question_id=new_question.id,
                        answer_text=answer_text,
                        is_primary=False,
                    )
                )

            stats["added"] += 1

        # =========================================================
        # COMMIT
        # =========================================================
        db.commit()

        # =========================================================
        # REPORT
        # =========================================================
        print_validation_report(
            filename,
            validator,
        )

        print("\n=== DB IMPORT SUMMARY ===")
        print(f"Total records      : {stats['total']}")
        print(f"Added              : {stats['added']}")
        print(
            f"Skipped (duplicate): "
            f"{stats['skipped_duplicate']}"
        )
        print(
            f"Skipped (rejected) : "
            f"{stats['skipped_rejected']}"
        )

        print("--- Status Details ---")
        print(
            f"Needs review       : "
            f"{stats['needs_review']}"
        )
        print(
            f"Ready              : "
            f"{stats['ready']}"
        )
        print(
            f"Draft               : "
            f"{stats['draft']}"
        )

        print("--- Content Types ---")
        print(
            f"Compound records   : "
            f"{stats['compound']}"
        )
        print(
            f"Jeopardy records   : "
            f"{stats['jeopardy']}"
        )
        print(
            f"Media-dependent    : "
            f"{stats['media_dependent']}"
        )

        print("======================\n")

    except Exception as exc:
        db.rollback()

        print(
            "\n❌ Import failed. "
            "Transaction rolled back."
        )
        print(f"Error: {exc}\n")

        raise

    finally:
        db.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="ZakoWhat canonical question importer"
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate canonical JSON without touching the database",
    )

    parser.add_argument(
        "json_file",
        nargs="?",
        default="canonical_1.json",
        help="Canonical JSON file to process",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.validate_only:
        validate_only(args.json_file)
    else:
        import_questions(args.json_file)