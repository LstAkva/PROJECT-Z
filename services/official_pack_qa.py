"""Read-only integrity checks for the staged Official Classic Zakovat packs.

This module deliberately validates metadata and launch-safety invariants only.  A
passing report means that the database representation is coherent; it is *not*
an editorial finding that a question or answer is factually correct.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, joinedload, selectinload

from models import AcceptedAnswer, Question, Quiz, QuizVersion, Round, RoundQuestion


OFFICIAL_SOURCE = "Zakovatklubi.uz — Ochiq savollar bazasi"
OFFICIAL_SOURCE_URL = "https://zakovatklubi.uz/open-question"
OFFICIAL_SOURCE_FILE = "zakovatklubi Base.pdf"

PACK_UNITS = {
    "Classic Zakovat — Pack 01": 4,
    "Classic Zakovat — Pack 02": 16,
    "Classic Zakovat — Pack 03": 24,
    "Classic Zakovat — Pack 04": 25,
    "Classic Zakovat — Pack 05": 26,
    "Classic Zakovat — Pack 06": 27,
    "Classic Zakovat — Pack 07": 28,
    "Classic Zakovat — Pack 08": 30,
}

# Target sequence -> actual donor provenance.  All other positions must retain
# their selected source unit and original question number.
REPLACEMENT_MATRIX = {
    ("Classic Zakovat — Pack 01", 1): (5, 3),
    ("Classic Zakovat — Pack 01", 20): (5, 9),
    ("Classic Zakovat — Pack 02", 22): (5, 4),
    ("Classic Zakovat — Pack 03", 6): (6, 3),
    ("Classic Zakovat — Pack 04", 16): (6, 9),
    ("Classic Zakovat — Pack 05", 13): (7, 2),
    ("Classic Zakovat — Pack 07", 4): (7, 6),
    ("Classic Zakovat — Pack 07", 18): (11, 2),
    ("Classic Zakovat — Pack 08", 2): (8, 2),
    ("Classic Zakovat — Pack 08", 3): (8, 10),
    ("Classic Zakovat — Pack 08", 8): (11, 11),
}

# These markers intentionally identify only unmistakable dependencies on a
# missing asset.  They do not attempt to make editorial judgments about prose.
MEDIA_DEPENDENCY_RE = re.compile(
    r"\b(?:"
    r"tarqatma(?:\s+material)?(?:da(?:gi)?)?|"
    r"(?:ushbu|quyidagi|mazkur)\s+(?:surat|rasm|foto(?:surat)?|tasvir)(?:da(?:gi)?|ga)?|"
    r"(?:suratdagi|rasmdagi|tasvirdagi|fotosuratdagi)|"
    r"(?:surat|rasm|foto|tasvir|ekran)ga\s+qarang|"
    r"(?:surat|rasm|foto(?:surat)?|tasvir)(?:da(?:gi)?)\s+(?:ko['\u2018\u2019\u02bb]?r(?:satilgan|ib)|aks\s+et(?:tirilgan|gan))|"
    r"(?:audio(?:yozuv)?|ovozli\s+yozuv)(?:ni)?\s+tinglang|"
    r"(?:video(?:lavha)?|klip)(?:ni)?\s+(?:tomosha\s+qiling|ko['\u2018\u2019\u02bb]?ring)|"
    r"ekrandagi\s+(?:video|tasvir|surat|rasm)"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class PackResult:
    title: str
    question_count: int
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


def _expected_provenance(pack_title: str, sequence: int) -> tuple[int, int]:
    return REPLACEMENT_MATRIX.get((pack_title, sequence), (PACK_UNITS[pack_title], sequence))


def _normalise_question_text(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _question_media_errors(question: Question) -> list[str]:
    errors = []
    if question.question_type != "text":
        errors.append(f"question {question.id}: expected text question_type")
    if question.media_url or question.media_provider:
        errors.append(f"question {question.id}: attached media is not allowed for this text-only launch set")
    if MEDIA_DEPENDENCY_RE.search(question.text or ""):
        errors.append(f"question {question.id}: obvious missing-media marker in question text")
    return errors


def run_official_pack_qa(db: Session) -> dict[str, Any]:
    """Run a non-mutating QA audit and return a JSON-serialisable report."""
    results: list[PackResult] = []
    all_question_ids: list[int] = []
    normalised_texts: list[tuple[int, str]] = []

    matching_quizzes = (
        db.query(Quiz)
        .filter(Quiz.title.in_(PACK_UNITS))
        .options(
            selectinload(Quiz.versions)
            .selectinload(QuizVersion.rounds)
            .selectinload(Round.round_questions)
            .joinedload(RoundQuestion.question)
            .selectinload(Question.accepted_answers)
        )
        .order_by(Quiz.title.asc())
        .all()
    )
    by_title = {quiz.title: quiz for quiz in matching_quizzes}

    staged_title_count = db.query(Quiz).filter(Quiz.title.like("Classic Zakovat — Pack %")).count()
    global_errors: list[str] = []
    if staged_title_count != len(PACK_UNITS):
        global_errors.append(
            f"expected exactly {len(PACK_UNITS)} staged Classic Zakovat packs, found {staged_title_count}"
        )

    for title, source_unit in PACK_UNITS.items():
        errors: list[str] = []
        quiz = by_title.get(title)
        if quiz is None:
            results.append(PackResult(title=title, question_count=0, errors=["quiz is missing"]))
            continue

        versions = list(quiz.versions)
        if len(versions) != 1:
            errors.append(f"expected exactly one version, found {len(versions)}")
            results.append(PackResult(title=title, question_count=0, errors=errors))
            continue

        version = versions[0]
        if version.status != "draft":
            errors.append(f"version {version.id}: expected draft status, found {version.status!r}")
        if version.game_mode != "classic_zakovat":
            errors.append(f"version {version.id}: expected classic_zakovat mode")
        if version.published_manifest is not None or version.published_at is not None:
            errors.append(f"version {version.id}: a staged pack must not have publication data")

        rounds = sorted(version.rounds, key=lambda r: r.sequence)
        if len(rounds) != 2:
            errors.append(f"expected 2 canonical staging rounds (1-tur & 2-tur), found {len(rounds)}")
            results.append(PackResult(title=title, question_count=0, errors=errors))
            continue

        pack_placements_count = 0
        for r_idx, round_ in enumerate(rounds, start=1):
            if round_.sequence != r_idx or round_.round_type != "zakovat_classic":
                errors.append(f"expected staging round {r_idx} of type zakovat_classic, found sequence {round_.sequence} type {round_.round_type}")
            config = round_.config or {}
            if config.get("source_unit") != source_unit:
                errors.append(f"round {r_idx} provenance source_unit must be {source_unit}")
            if OFFICIAL_SOURCE not in str(config.get("provenance", "")):
                errors.append(f"round {r_idx} provenance does not identify the official source")

            placements = sorted(round_.round_questions, key=lambda rq: rq.sequence)
            sequences = [placement.sequence for placement in placements]
            if len(placements) != 12:
                errors.append(f"round {r_idx} expected 12 questions, found {len(placements)}")
            if sequences != list(range(1, 13)):
                errors.append(f"round {r_idx} question sequence must be exactly 1..12")

            pack_placements_count += len(placements)

            for placement in placements:
                question = placement.question
                if question is None:
                    errors.append(f"round {r_idx} sequence {placement.sequence}: referenced question is missing")
                    continue
                all_question_ids.append(question.id)
                normalised_texts.append((question.id, _normalise_question_text(question.text)))

                if question.round_id is not None or question.sequence is not None:
                    errors.append(f"question {question.id}: legacy round linkage must be NULL")
                if question.status != "draft":
                    errors.append(f"question {question.id}: expected draft status")
                errors.extend(_question_media_errors(question))

                answers = question.accepted_answers
                primaries = [answer for answer in answers if answer.is_primary and answer.answer_text and answer.answer_text.strip()]
                if len(primaries) != 1:
                    errors.append(f"question {question.id}: expected exactly one non-empty primary answer")

                overall_sequence = (r_idx - 1) * 12 + placement.sequence
                source_meta = question.source_meta or {}
                expected_unit, expected_question = _expected_provenance(title, overall_sequence)
                expected_meta = {
                    "source": OFFICIAL_SOURCE,
                    "source_url": OFFICIAL_SOURCE_URL,
                    "source_file": OFFICIAL_SOURCE_FILE,
                    "unit_number": expected_unit,
                    "question_number": expected_question,
                }
                for field, expected_value in expected_meta.items():
                    if source_meta.get(field) != expected_value:
                        errors.append(
                            f"question {question.id}: provenance {field} must be {expected_value!r}, "
                            f"found {source_meta.get(field)!r}"
                        )

        results.append(PackResult(title=title, question_count=pack_placements_count, errors=errors))

    duplicate_ids = [question_id for question_id, count in Counter(all_question_ids).items() if count > 1]
    if duplicate_ids:
        global_errors.append(f"questions are reused across staged placements: {duplicate_ids}")
    duplicate_text_ids = [
        question_id
        for question_id, text in normalised_texts
        if text and sum(candidate == text for _, candidate in normalised_texts) > 1
    ]
    if duplicate_text_ids:
        global_errors.append(f"exact duplicate staged question text detected for question IDs: {sorted(set(duplicate_text_ids))}")
    if len(set(all_question_ids)) != 192:
        global_errors.append(f"expected 192 distinct staged questions, found {len(set(all_question_ids))}")

    report_results = [
        {
            "title": result.title,
            "status": "PASS" if result.passed else "FAIL",
            "questions": result.question_count,
            "errors": result.errors,
        }
        for result in results
    ]
    return {
        "status": "PASS" if not global_errors and all(result.passed for result in results) else "FAIL",
        "packs": report_results,
        "total_questions": len(set(all_question_ids)),
        "global_errors": global_errors,
        "editorial_note": (
            "This report validates technical integrity and launch safety only. "
            "It does not certify factual accuracy, answer fairness, or editorial approval."
        ),
    }
