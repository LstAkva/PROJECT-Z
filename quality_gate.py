import json
import os
import re
import sys
from collections import Counter
from typing import Dict, Any, List, Tuple


CYRILLIC_PATTERN = re.compile(r'[\u0400-\u04FF]')
CONTAMINATION_PATTERN = re.compile(r'\b(javob|izoh|javobi)\s*:', re.IGNORECASE)
SENTENCE_ENDINGS = ('.', '?', '!', '"', "'", '»', '”', '’', ')', ':', '…')


def evaluate_record(record: Dict[str, Any], index: int) -> Tuple[str, List[str]]:
    """
    Evaluates a canonical question record according to ZakoWhat Quality Gate rules.
    Returns (status, reasons) where status is 'rejected', 'needs_review', or 'ready_to_import'.
    Deterministic rules: No auto-transliteration or LLM guessing.
    """
    reasons = []

    text = record.get("text") or ""
    primary_answer = record.get("primary_answer") or ""
    points = record.get("points")

    text_clean = str(text).strip()
    primary_clean = str(primary_answer).strip()

    # =========================================================================
    # 1. REJECTED CRITERIA
    # =========================================================================
    if not text_clean:
        reasons.append("empty_question_text")
    if not primary_clean:
        reasons.append("empty_primary_answer")

    # Cyrillic checks (separate flags for question vs primary answer)
    if primary_clean and CYRILLIC_PATTERN.search(primary_clean):
        reasons.append("cyrillic_in_primary_answer")

    if text_clean and CYRILLIC_PATTERN.search(text_clean):
        reasons.append("cyrillic_in_question")

    # Invalid / non-positive points
    if points is None:
        reasons.append("missing_points")
    elif not isinstance(points, (int, float)) or points <= 0:
        reasons.append("non_positive_points")

    # Length of primary answer > 120 characters
    if len(primary_clean) > 120:
        reasons.append("answer_too_long_gt_120")

    # Provenance verification
    source = record.get("source")
    if not isinstance(source, dict):
        reasons.append("missing_provenance")
    else:
        has_tg = bool(source.get("channel_id") and source.get("question_message_id"))
        has_file = bool(source.get("source_file") and source.get("question_number"))
        has_src = bool(source.get("source_name"))
        if not (has_tg or has_file or has_src):
            reasons.append("missing_provenance")

    # Answer / explanation contamination in question text
    if CONTAMINATION_PATTERN.search(text_clean):
        reasons.append("answer_contamination_in_question")

    # Accepted answers structure validation
    accepted_answers = record.get("accepted_answers")
    if accepted_answers is not None:
        if not isinstance(accepted_answers, list):
            reasons.append("malformed_accepted_answers")
        else:
            for ans_item in accepted_answers:
                if not isinstance(ans_item, dict) or not str(ans_item.get("text") or "").strip():
                    reasons.append("malformed_accepted_answers")
                    break

    if reasons:
        return "rejected", reasons

    # =========================================================================
    # 2. NEEDS REVIEW CRITERIA
    # =========================================================================
    # Options / ambiguous formatting in primary answer
    lower_ans = primary_clean.lower()
    if any(keyword in lower_ans for keyword in (" yoki ", " yoxud ", "/", "\\", "(", ")")):
        reasons.append("answer_contains_options_or_brackets")

    # Length of question < 10 characters
    if len(text_clean) < 10:
        reasons.append("question_too_short_lt_10")

    # Missing sentence-ending punctuation
    if not text_clean.endswith(SENTENCE_ENDINGS):
        reasons.append("missing_terminal_punctuation")

    # Editorial flags or media attachments
    editorial = record.get("editorial", {})
    if isinstance(editorial, dict):
        flags = editorial.get("flags", []) or []
        if "missing_media" in flags or "media_dependent" in flags:
            reasons.append("media_dependent_flag")
    if record.get("media") or record.get("missing_media"):
        if "media_dependent_flag" not in reasons:
            reasons.append("media_dependent_flag")

    # Potential answer leak in question text (primary answer found inside question)
    if len(primary_clean) >= 5 and primary_clean.lower() in text_clean.lower():
        reasons.append("potential_answer_leak")

    if reasons:
        return "needs_review", reasons

    # =========================================================================
    # 3. READY TO IMPORT
    # =========================================================================
    return "ready_to_import", []


def run_quality_gate(input_filepath: str = "canonical_1.json", output_filepath: str = "quality_report.json"):
    if not os.path.exists(input_filepath):
        print(f"Error: Input file '{input_filepath}' does not exist.")
        sys.exit(1)

    print(f"Loading '{input_filepath}'...")
    with open(input_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        print(f"Error: Expected JSON array in '{input_filepath}'.")
        sys.exit(1)

    total_count = len(data)
    print(f"Total canonical records: {total_count}")

    buckets = {
        "ready_to_import": [],
        "needs_review": [],
        "rejected": []
    }

    rejection_reasons = Counter()
    review_reasons = Counter()

    for idx, record in enumerate(data):
        status, reasons = evaluate_record(record, idx)

        item_summary = {
            "index": idx,
            "id": record.get("id"),
            "points": record.get("points"),
            "text": (record.get("text") or "")[:100],
            "primary_answer": record.get("primary_answer"),
            "reasons": reasons,
            "record": record
        }

        buckets[status].append(item_summary)

        if status == "rejected":
            for r in reasons:
                rejection_reasons[r] += 1
        elif status == "needs_review":
            for r in reasons:
                review_reasons[r] += 1

    report = {
        "metadata": {
            "source_file": input_filepath,
            "total_records": total_count,
            "ready_to_import_count": len(buckets["ready_to_import"]),
            "ready_to_import_pct": round(len(buckets["ready_to_import"]) / total_count * 100, 2),
            "needs_review_count": len(buckets["needs_review"]),
            "needs_review_pct": round(len(buckets["needs_review"]) / total_count * 100, 2),
            "rejected_count": len(buckets["rejected"]),
            "rejected_pct": round(len(buckets["rejected"]) / total_count * 100, 2),
        },
        "rejection_breakdown": dict(rejection_reasons.most_common()),
        "review_breakdown": dict(review_reasons.most_common()),
        "ready_to_import_ids": [item["id"] for item in buckets["ready_to_import"]],
        "needs_review_sample": buckets["needs_review"][:20],
        "rejected_sample": buckets["rejected"][:20],
    }

    with open(output_filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 65)
    print("           ZAKOWHAT QUALITY GATE REPORT")
    print("=" * 65)
    print(f"Total Processed:    {total_count}")
    print(f"Ready to Import:    {len(buckets['ready_to_import']):>5} ({report['metadata']['ready_to_import_pct']:>5}%)")
    print(f"Needs Review:       {len(buckets['needs_review']):>5} ({report['metadata']['needs_review_pct']:>5}%)")
    print(f"Rejected:           {len(buckets['rejected']):>5} ({report['metadata']['rejected_pct']:>5}%)")
    print("-" * 65)
    print("Top Rejection Reasons:")
    for reason, cnt in rejection_reasons.most_common(5):
        print(f"  - {reason:<35}: {cnt}")
    print("\nTop Review Triggers:")
    for reason, cnt in review_reasons.most_common(5):
        print(f"  - {reason:<35}: {cnt}")
    print("=" * 65)
    print(f"\nDetailed report saved to: {output_filepath}\n")

    return report


if __name__ == "__main__":
    filepath = sys.argv[1] if len(sys.argv) > 1 else "canonical_1.json"
    run_quality_gate(filepath)
