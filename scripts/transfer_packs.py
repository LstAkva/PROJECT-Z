"""
One-off content transfer: copy published packs from the DEVELOPMENT database (ep-damp-frog)
into a TARGET database (a temporary branch of production, and only later — with separate
approval — production itself).

What is copied, per pack: quiz -> its latest published version -> rounds -> round_questions ->
questions -> accepted_answers. Every row gets a NEW target id; the published_manifest (the
immutable playable snapshot, which embeds quiz/version/round/question/round_question/answer ids
that gameplay writes into answer records) is rewritten with the new ids.

Never copied: users, attempts, answer records, daily questions/submissions, test packs, drafts.

Performance design (v2): the source is read in a handful of bulk queries into memory and its
connection is closed BEFORE the target transaction starts; the target is written table by table
in batches and validated with bulk queries. The target transaction therefore needs only a few
dozen round trips instead of thousands, and both connections bypass the Neon pooler.

Safety:
  * Default is a DRY RUN: everything runs inside one transaction and is ROLLED BACK.
    Pass --commit to persist (still one transaction, all-or-nothing).
  * Idempotent: each copied version carries manifest["transfer_provenance"]; a pack already
    transferred from the same source is skipped, never duplicated.
  * Source is opened READ ONLY and must be endpoint ep-damp-frog-b1y9sc7x.
  * Target must be in Neon project old-bird-43200046, at Alembic head b2c3d4e5f6a7, must not be
    the source, and must NOT be the production branch unless
    --production-branch-approved br-small-base-b1pev96p is passed explicitly.
  * Validation runs before commit; any failure rolls everything back.

Usage (target URL is read from the TRANSFER_TARGET_DATABASE_URL environment variable / .env,
never from the command line, so it does not end up in shell history):
    python scripts/transfer_packs.py            # dry run
    python scripts/transfer_packs.py --commit   # persist on the (temporary branch) target
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, func, inspect as sa_inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import normalize_database_url  # noqa: E402
from models import (  # noqa: E402
    AcceptedAnswer,
    AnswerRecord,
    DailyQuestion,
    DailySubmission,
    Question,
    Quiz,
    QuizVersion,
    Round,
    RoundQuestion,
    SoloAttempt,
    User,
)

# --------------------------------------------------------------------------------------
# Fixed scope: exactly these packs, identified by source id AND title.
# --------------------------------------------------------------------------------------
EXPECTED_PACKS: Dict[int, str] = {
    43: "Classic Zakovat — Pack 01",
    44: "Classic Zakovat — Pack 02",
    45: "Classic Zakovat — Pack 03",
    46: "Classic Zakovat — Pack 04",
    47: "Classic Zakovat — Pack 05",
    81: "Svoya Igra — O‘zbekiston Bo‘ylab",
    82: "Svoya Igra — Jahon Tarixi va Hukmdorlar",
    83: "Svoya Igra — Sayyora Geografiyasi",
    84: "Svoya Igra — Madaniyat, Sanʼat va Sport",
    85: "Svoya Igra — So‘zlar, Sonlar va Matritsalar",
}
FORBIDDEN_SOURCE_IDS = {86, 87}

# Second approved batch (owner instruction 2026-11-10): Classic Zakovat Pack 06-08.
PACKS_06_08: Dict[int, str] = {
    48: "Classic Zakovat — Pack 06",
    49: "Classic Zakovat — Pack 07",
    50: "Classic Zakovat — Pack 08",
}
BATCHES: Dict[str, Dict[int, str]] = {"rc1": EXPECTED_PACKS, "packs-06-08": PACKS_06_08}
APPROVED_PACKS: Dict[int, str] = {**EXPECTED_PACKS, **PACKS_06_08}

SOURCE_ENDPOINT = "ep-damp-frog-b1y9sc7x"
NEON_PROJECT = "old-bird-43200046"
PRODUCTION_BRANCH = "br-small-base-b1pev96p"
ALEMBIC_HEAD = "b2c3d4e5f6a7"
PROVENANCE_KEY = "transfer_provenance"

CONTENT_MODELS = [Quiz, QuizVersion, Round, RoundQuestion, Question, AcceptedAnswer]
PROTECTED_MODELS = [User, SoloAttempt, AnswerRecord, DailyQuestion, DailySubmission]

QUESTION_FIELDS = [
    "text", "media_provider", "media_url", "points", "default_points", "explanation", "status",
    "compound_group_id", "compound_type", "category", "options", "question_type",
]


class TransferError(RuntimeError):
    pass


def _row(obj) -> Dict[str, Any]:
    """Plain dict of an ORM row's column values (no lazy loading afterwards)."""
    return {c.key: copy.deepcopy(getattr(obj, c.key)) for c in sa_inspect(obj).mapper.column_attrs}


# --------------------------------------------------------------------------------------
# Bulk snapshot of packs (used for the source AND to validate the target)
# --------------------------------------------------------------------------------------
def load_snapshot(db: Session, version_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """
    version_id -> {"version", "quiz", "rounds": [{"round", "rqs": [{"rq", "question", "answers"}]}]}
    Six bulk queries in total, independent of pack size.
    """
    if not version_ids:
        return {}
    versions = {v.id: _row(v) for v in db.scalars(select(QuizVersion).where(QuizVersion.id.in_(version_ids)))}
    quizzes = {q.id: _row(q) for q in db.scalars(select(Quiz).where(Quiz.id.in_({v["quiz_id"] for v in versions.values()})))}
    rounds = [_row(r) for r in db.scalars(select(Round).where(Round.quiz_version_id.in_(version_ids)))]
    round_ids = [r["id"] for r in rounds]
    rqs = [_row(rq) for rq in db.scalars(select(RoundQuestion).where(RoundQuestion.round_id.in_(round_ids)))] if round_ids else []
    question_ids = {rq["question_id"] for rq in rqs}
    questions = {q.id: _row(q) for q in db.scalars(select(Question).where(Question.id.in_(question_ids)))} if question_ids else {}
    answers: Dict[int, List[Dict[str, Any]]] = {}
    if question_ids:
        for a in db.scalars(select(AcceptedAnswer).where(AcceptedAnswer.question_id.in_(question_ids)).order_by(AcceptedAnswer.id)):
            answers.setdefault(a.question_id, []).append(_row(a))

    out: Dict[int, Dict[str, Any]] = {}
    for vid, v in versions.items():
        pack_rounds = sorted((r for r in rounds if r["quiz_version_id"] == vid), key=lambda r: (r["sequence"], r["id"]))
        rendered = []
        for r in pack_rounds:
            r_rqs = sorted((rq for rq in rqs if rq["round_id"] == r["id"]), key=lambda rq: (rq["sequence"], rq["id"]))
            rendered.append({
                "round": r,
                "rqs": [{"rq": rq, "question": questions[rq["question_id"]], "answers": answers.get(rq["question_id"], [])} for rq in r_rqs],
            })
        out[vid] = {"version": v, "quiz": quizzes[v["quiz_id"]], "rounds": rendered}
    return out


def manifest_rounds(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Exactly what api.quizzes.compile_published_manifest produces for 'rounds' (round_questions path)."""
    out = []
    for r in pack["rounds"]:
        qs = []
        for item in r["rqs"]:
            rq, q = item["rq"], item["question"]
            pts = rq["points_override"] if rq["points_override"] is not None else (
                q["default_points"] if q["default_points"] is not None else q["points"])
            qs.append({
                "round_question_id": rq["id"],
                "question_id": q["id"],
                "sequence": rq["sequence"],
                "text": q["text"],
                "explanation": q["explanation"],
                "media_provider": q["media_provider"],
                "media_url": q["media_url"],
                "question_type": q["question_type"] or "text",
                "options": q["options"],
                "points": pts if pts is not None else 1,
                "config": rq["config_override"] or {},
                "accepted_answers": [{"id": a["id"], "answer_text": a["answer_text"], "is_primary": a["is_primary"]} for a in item["answers"]],
            })
        out.append({
            "round_id": r["round"]["id"],
            "sequence": r["round"]["sequence"],
            "round_type": r["round"]["round_type"],
            "config": r["round"]["config"] or {},
            "questions": qs,
        })
    return out


def pack_signature(pack: Dict[str, Any]) -> Dict[str, Any]:
    """Id-free description of a pack: everything that affects play and scoring."""
    return {
        "title": pack["quiz"]["title"],
        "description": pack["quiz"]["description"],
        "version_number": pack["version"]["version_number"],
        "status": pack["version"]["status"],
        "game_mode": pack["version"]["game_mode"],
        "rounds": [
            {
                "sequence": r["round"]["sequence"],
                "round_type": r["round"]["round_type"],
                "config": r["round"]["config"],
                "items": [
                    {
                        "sequence": it["rq"]["sequence"],
                        "points_override": it["rq"]["points_override"],
                        "config_override": it["rq"]["config_override"],
                        "question": {k: it["question"][k] for k in QUESTION_FIELDS},
                        "accepted_answers": [(a["answer_text"], bool(a["is_primary"])) for a in it["answers"]],
                    }
                    for it in r["rqs"]
                ],
            }
            for r in pack["rounds"]
        ],
    }


def strip_volatile(manifest: Dict[str, Any]) -> Dict[str, Any]:
    m = copy.deepcopy(manifest)
    m.pop("published_at", None)
    m.pop(PROVENANCE_KEY, None)
    return m


# --------------------------------------------------------------------------------------
# Target helpers
# --------------------------------------------------------------------------------------
def table_counts(db: Session) -> Dict[str, int]:
    parts = [select(func.count()).select_from(m.__table__).scalar_subquery().label(m.__tablename__)
             for m in CONTENT_MODELS + PROTECTED_MODELS]
    row = db.execute(select(*parts)).one()
    return dict(row._mapping)


def find_transferred(db: Session, source_endpoint: str) -> Dict[int, Dict[str, Any]]:
    """source quiz id -> {'version_id', 'quiz_id'} of a target version carrying our provenance marker."""
    found: Dict[int, Dict[str, Any]] = {}
    rows = db.execute(select(QuizVersion.id, QuizVersion.quiz_id, QuizVersion.published_manifest)
                      .where(QuizVersion.published_manifest.isnot(None))).all()
    for vid, qid, manifest in rows:
        prov = (manifest or {}).get(PROVENANCE_KEY) or {}
        if prov.get("source_endpoint") == source_endpoint and prov.get("source_quiz_id") is not None:
            found[int(prov["source_quiz_id"])] = {"version_id": vid, "quiz_id": qid}
    return found


# --------------------------------------------------------------------------------------
# Step 1: read + pre-flight the source (no target involved)
# --------------------------------------------------------------------------------------
def load_source(src: Session, pack_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
    """source quiz id -> pack snapshot. Raises TransferError on anything unexpected."""
    pack_ids = list(pack_ids)
    for qid in pack_ids:
        if qid in FORBIDDEN_SOURCE_IDS:
            raise TransferError(f"Source quiz {qid} is a test pack and must never be transferred.")
        if qid not in APPROVED_PACKS:
            raise TransferError(f"Source quiz {qid} is not in the approved pack list.")

    quizzes = {q.id: q.title for q in src.scalars(select(Quiz).where(Quiz.id.in_(pack_ids)))}
    published = src.execute(
        select(QuizVersion.quiz_id, QuizVersion.id, QuizVersion.version_number)
        .where(QuizVersion.quiz_id.in_(pack_ids), QuizVersion.status == "published")
    ).all()
    latest: Dict[int, tuple] = {}
    for quiz_id, vid, num in published:
        if quiz_id not in latest or num > latest[quiz_id][1]:
            latest[quiz_id] = (vid, num)

    for qid in pack_ids:
        if qid not in quizzes:
            raise TransferError(f"Source quiz {qid} not found.")
        if quizzes[qid] != APPROVED_PACKS[qid]:
            raise TransferError(f"Source quiz {qid} title mismatch: {quizzes[qid]!r} != {APPROVED_PACKS[qid]!r}")
        if qid not in latest:
            raise TransferError(f"Source quiz {qid} has no published version.")

    snaps = load_snapshot(src, [latest[qid][0] for qid in pack_ids])
    by_quiz: Dict[int, Dict[str, Any]] = {}
    for qid in pack_ids:
        pack = snaps[latest[qid][0]]
        manifest = pack["version"]["published_manifest"]
        if not manifest:
            raise TransferError(f"Source quiz {qid} has no published manifest.")
        if not pack["rounds"] or any(not r["rqs"] for r in pack["rounds"]):
            raise TransferError(f"Source quiz {qid} has an empty or legacy (no round_questions) round; not supported.")
        if any(not it["answers"] for r in pack["rounds"] for it in r["rqs"]):
            raise TransferError(f"Source quiz {qid} has a question without accepted answers.")
        # The stored snapshot must equal what the rows compile to (otherwise players see other content).
        if manifest_rounds(pack) != manifest.get("rounds"):
            raise TransferError(f"Source quiz {qid}: stored manifest rounds differ from the database rows (drift).")
        by_quiz[qid] = pack
    return by_quiz


# --------------------------------------------------------------------------------------
# Step 2: write the target in batches (inside the caller's transaction)
# --------------------------------------------------------------------------------------
def write_packs(tgt: Session, packs: List[Dict[str, Any]], source_endpoint: str, transferred_at: str) -> List[Dict[str, Any]]:
    # 1. quizzes
    new_quizzes = [Quiz(title=p["quiz"]["title"], description=p["quiz"]["description"], created_at=p["quiz"]["created_at"]) for p in packs]
    tgt.add_all(new_quizzes)
    tgt.flush()
    # 2. versions (manifest set later, once all ids exist)
    new_versions = [
        QuizVersion(quiz_id=nq.id, version_number=p["version"]["version_number"], status="published",
                    game_mode=p["version"]["game_mode"], published_at=p["version"]["published_at"], published_manifest=None)
        for p, nq in zip(packs, new_quizzes)
    ]
    tgt.add_all(new_versions)
    tgt.flush()
    # 3. rounds
    round_pairs = []
    for p, nv in zip(packs, new_versions):
        for r in p["rounds"]:
            round_pairs.append((r["round"]["id"], Round(quiz_version_id=nv.id, sequence=r["round"]["sequence"],
                                                        round_type=r["round"]["round_type"], config=copy.deepcopy(r["round"]["config"]))))
    tgt.add_all([nr for _, nr in round_pairs])
    tgt.flush()
    round_map = {sid: nr.id for sid, nr in round_pairs}
    # 4. questions (a question shared by several packs is copied once)
    question_pairs, seen = [], set()
    for p in packs:
        for r in p["rounds"]:
            for it in r["rqs"]:
                q = it["question"]
                if q["id"] in seen:
                    continue
                seen.add(q["id"])
                meta = copy.deepcopy(q["source_meta"]) if isinstance(q["source_meta"], dict) else (
                    {} if q["source_meta"] is None else {"original": q["source_meta"]})
                meta[PROVENANCE_KEY] = {"source_endpoint": source_endpoint, "source_question_id": q["id"], "transferred_at": transferred_at}
                question_pairs.append((q["id"], Question(
                    round_id=round_map.get(q["round_id"]) if q["round_id"] is not None else None,
                    sequence=q["sequence"], source_meta=meta, **{k: copy.deepcopy(q[k]) for k in QUESTION_FIELDS},
                )))
    tgt.add_all([nq for _, nq in question_pairs])
    tgt.flush()
    question_map = {sid: nq.id for sid, nq in question_pairs}
    # 5. accepted answers
    answer_pairs, seen = [], set()
    for p in packs:
        for r in p["rounds"]:
            for it in r["rqs"]:
                if it["question"]["id"] in seen:
                    continue
                seen.add(it["question"]["id"])
                for a in it["answers"]:
                    answer_pairs.append((a["id"], AcceptedAnswer(question_id=question_map[a["question_id"]],
                                                                 answer_text=a["answer_text"], is_primary=a["is_primary"])))
    tgt.add_all([na for _, na in answer_pairs])
    tgt.flush()
    answer_map = {sid: na.id for sid, na in answer_pairs}
    # 6. round questions
    rq_pairs = []
    for p in packs:
        for r in p["rounds"]:
            for it in r["rqs"]:
                rq = it["rq"]
                rq_pairs.append((rq["id"], RoundQuestion(
                    round_id=round_map[rq["round_id"]], question_id=question_map[rq["question_id"]], sequence=rq["sequence"],
                    points_override=rq["points_override"], config_override=copy.deepcopy(rq["config_override"]), created_at=rq["created_at"],
                )))
    tgt.add_all([n for _, n in rq_pairs])
    tgt.flush()
    rq_map = {sid: n.id for sid, n in rq_pairs}
    # 7. rewrite each stored snapshot with target ids
    copied = []
    for p, nq, nv in zip(packs, new_quizzes, new_versions):
        manifest = copy.deepcopy(p["version"]["published_manifest"])
        manifest["quiz_id"], manifest["version_id"] = nq.id, nv.id
        for rnd in manifest.get("rounds", []):
            rnd["round_id"] = round_map[rnd["round_id"]]
            for q in rnd.get("questions", []):
                q["question_id"] = question_map[q["question_id"]]
                q["round_question_id"] = rq_map[q["round_question_id"]]
                for ans in q.get("accepted_answers", []) or []:
                    ans["id"] = answer_map[ans["id"]]
        manifest[PROVENANCE_KEY] = {"source_endpoint": source_endpoint, "source_quiz_id": p["quiz"]["id"],
                                    "source_version_id": p["version"]["id"], "transferred_at": transferred_at}
        nv.published_manifest = manifest
        copied.append({
            "source_quiz_id": p["quiz"]["id"], "target_quiz_id": nq.id, "target_version_id": nv.id,
            "title": p["quiz"]["title"], "game_mode": p["version"]["game_mode"],
            "rounds": len(p["rounds"]), "round_questions": sum(len(r["rqs"]) for r in p["rounds"]),
        })
    tgt.flush()
    return copied


# --------------------------------------------------------------------------------------
# Step 3: validate the target (bulk, inside the transaction, before commit)
# --------------------------------------------------------------------------------------
def validate_packs(source_packs: Dict[int, Dict[str, Any]], tgt: Session, transferred: Dict[int, Dict[str, Any]],
                   pack_ids: List[int]) -> List[str]:
    errors: List[str] = []
    missing = [qid for qid in pack_ids if qid not in transferred]
    errors.extend(f"pack {qid} missing on target after transfer" for qid in missing)
    present = [qid for qid in pack_ids if qid in transferred]
    tgt.expire_all()
    target_snaps = load_snapshot(tgt, [transferred[qid]["version_id"] for qid in present])
    for qid in present:
        src_pack = source_packs[qid]
        tgt_pack = target_snaps.get(transferred[qid]["version_id"])
        label = f"pack {qid}->{transferred[qid]['quiz_id']}"
        if tgt_pack is None:
            errors.append(f"{label}: target version not found")
            continue
        # 1. Content and scoring identical (id-free comparison of rows).
        if pack_signature(src_pack) != pack_signature(tgt_pack):
            errors.append(f"{label}: content/scoring signature differs between source and target")
        tm = tgt_pack["version"]["published_manifest"] or {}
        sm = src_pack["version"]["published_manifest"] or {}
        # 2. Stored snapshot equals what the target rows compile to, and only ids/marker changed.
        if manifest_rounds(tgt_pack) != tm.get("rounds"):
            errors.append(f"{label}: target manifest rounds do not match target rows")
        for key in ("title", "description", "game_mode", "category", "visibility", "version_number", "published_at"):
            if tm.get(key) != sm.get(key):
                errors.append(f"{label}: manifest field {key!r} changed")
        if tm.get("quiz_id") != tgt_pack["quiz"]["id"] or tm.get("version_id") != tgt_pack["version"]["id"]:
            errors.append(f"{label}: manifest quiz/version ids not rewritten")
        if (tm.get(PROVENANCE_KEY) or {}).get("source_quiz_id") != qid:
            errors.append(f"{label}: provenance marker missing")
        # 3. Every referenced id belongs to this target pack and differs from nothing it should not.
        for r in tgt_pack["rounds"]:
            for it in r["rqs"]:
                if it["rq"]["round_id"] != r["round"]["id"] or it["rq"]["question_id"] != it["question"]["id"]:
                    errors.append(f"{label}: round_question {it['rq']['id']} relationship broken")
                if any(a["question_id"] != it["question"]["id"] for a in it["answers"]):
                    errors.append(f"{label}: accepted answer linked to wrong question")
                if not it["answers"]:
                    errors.append(f"{label}: question {it['question']['id']} has no accepted answers")
                meta = it["question"]["source_meta"] or {}
                if (meta.get(PROVENANCE_KEY) or {}).get("source_endpoint") is None:
                    errors.append(f"{label}: question {it['question']['id']} is not a transferred copy")
    return errors


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
def apply_transfer(source_packs: Dict[int, Dict[str, Any]], tgt: Session, source_endpoint: str) -> Dict[str, Any]:
    """Writes + validates inside the caller's target transaction. Never commits."""
    pack_ids = list(source_packs)
    transferred_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    before = table_counts(tgt)
    already = find_transferred(tgt, source_endpoint)
    to_copy = [qid for qid in pack_ids if qid not in already]
    skipped = [{"source_quiz_id": qid, "target_quiz_id": already[qid]["quiz_id"], "reason": "already transferred"}
               for qid in pack_ids if qid in already]
    copied = write_packs(tgt, [source_packs[qid] for qid in to_copy], source_endpoint, transferred_at) if to_copy else []
    after = table_counts(tgt)

    expected_delta = {name: 0 for name in before}
    question_ids, answer_count = set(), 0
    for qid in to_copy:
        p = source_packs[qid]
        expected_delta["quizzes"] += 1
        expected_delta["quiz_versions"] += 1
        expected_delta["rounds"] += len(p["rounds"])
        expected_delta["round_questions"] += sum(len(r["rqs"]) for r in p["rounds"])
        for r in p["rounds"]:
            for it in r["rqs"]:
                if it["question"]["id"] not in question_ids:
                    question_ids.add(it["question"]["id"])
                    answer_count += len(it["answers"])
    expected_delta["questions"] = len(question_ids)
    expected_delta["accepted_answers"] = answer_count
    actual_delta = {name: after[name] - before[name] for name in before}

    errors: List[str] = []
    if actual_delta != expected_delta:
        errors.append(f"row count delta mismatch: expected {expected_delta}, got {actual_delta}")
    for m in PROTECTED_MODELS:
        if before[m.__tablename__] != after[m.__tablename__]:
            errors.append(f"protected table {m.__tablename__} changed")
    errors.extend(validate_packs(source_packs, tgt, find_transferred(tgt, source_endpoint), pack_ids))

    return {
        "transferred_at": transferred_at,
        "copied": copied,
        "skipped": skipped,
        "counts_before": before,
        "counts_after": after,
        "expected_delta": expected_delta,
        "actual_delta": actual_delta,
        "validation_errors": errors,
        "ok": not errors,
        "target_seconds": round(time.monotonic() - started, 2),
    }


def run_transfer(src: Session, tgt: Session, source_endpoint: str, pack_ids: Iterable[int] = tuple(EXPECTED_PACKS)) -> Dict[str, Any]:
    """Convenience wrapper (tests): load source, then write + validate on the target session."""
    return apply_transfer(load_source(src, pack_ids), tgt, source_endpoint)


# --------------------------------------------------------------------------------------
# CLI with environment guards
# --------------------------------------------------------------------------------------
def neon_identity(conn) -> Dict[str, Optional[str]]:
    return {
        k: conn.execute(text(f"select current_setting('neon.{k}', true)")).scalar()
        for k in ("project_id", "branch_id", "endpoint_id")
    }


def check_identities(src_id: Dict[str, Optional[str]], tgt_id: Dict[str, Optional[str]],
                     target_revision: Optional[str], production_branch_approved: Optional[str]) -> None:
    """Refuses anything but: dev source -> non-production branch of the production project at head."""
    if src_id.get("endpoint_id") != SOURCE_ENDPOINT:
        raise TransferError(f"Source must be {SOURCE_ENDPOINT}, got {src_id.get('endpoint_id')}.")
    if tgt_id.get("project_id") != NEON_PROJECT:
        raise TransferError(f"Target must be in Neon project {NEON_PROJECT}, got {tgt_id.get('project_id')}.")
    if not tgt_id.get("branch_id") or not tgt_id.get("endpoint_id"):
        raise TransferError("Target Neon branch/endpoint could not be identified.")
    if tgt_id.get("endpoint_id") == SOURCE_ENDPOINT or tgt_id.get("branch_id") == src_id.get("branch_id"):
        raise TransferError("Target is the source database.")
    if tgt_id.get("branch_id") == PRODUCTION_BRANCH and production_branch_approved != PRODUCTION_BRANCH:
        raise TransferError(
            "Target is the PRODUCTION branch. Refusing. This requires separate approval and "
            f"--production-branch-approved {PRODUCTION_BRANCH}."
        )
    if target_revision != ALEMBIC_HEAD:
        raise TransferError(f"Target Alembic revision is {target_revision}, expected {ALEMBIC_HEAD}.")


def direct_url(url: str) -> str:
    """Neon pooled host -> direct compute host (a long transaction should not go through the pooler)."""
    return url.replace("-pooler.", ".", 1)


def make_engine(url: str):
    return create_engine(
        direct_url(url), pool_pre_ping=True,
        connect_args={"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 5,
                      "connect_timeout": 20},
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true", help="Persist the transfer (default: dry run, rolled back).")
    parser.add_argument("--batch", choices=sorted(BATCHES), default="rc1",
                        help="Which approved batch of packs to transfer (default: rc1 = the original 10).")
    parser.add_argument("--production-branch-approved", default=None,
                        help=f"Required to target the production branch; must equal {PRODUCTION_BRANCH}.")
    args = parser.parse_args(argv)

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    source_url = normalize_database_url(os.getenv("DATABASE_URL"))
    target_url = normalize_database_url(os.getenv("TRANSFER_TARGET_DATABASE_URL"))
    if not source_url or not target_url:
        raise TransferError("DATABASE_URL (source) and TRANSFER_TARGET_DATABASE_URL (target) must both be set.")
    if not source_url.startswith("postgresql") or not target_url.startswith("postgresql"):
        raise TransferError("Both source and target must be PostgreSQL (Neon).")

    t0 = time.monotonic()
    # ---- Phase A: identify both sides, read the source, CLOSE the source connection. ----
    src_engine, tgt_engine = make_engine(source_url), make_engine(target_url)
    with src_engine.connect() as src_conn:
        src_conn.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
        src_conn.commit()
        if src_conn.execute(text("show transaction_read_only")).scalar() != "on":
            raise TransferError("Could not open the source read-only.")
        src_id = neon_identity(src_conn)
        with tgt_engine.connect() as probe:
            tgt_id = neon_identity(probe)
            revision = probe.execute(text("select version_num from alembic_version")).scalar()
        print(f"source: {src_id}")
        print(f"target: {tgt_id}")
        check_identities(src_id, tgt_id, revision, args.production_branch_approved)
        print(f"batch: {args.batch} -> source quizzes {sorted(BATCHES[args.batch])}")
        source_packs = load_source(Session(bind=src_conn), list(BATCHES[args.batch]))
        src_conn.rollback()
    src_engine.dispose()
    read_seconds = round(time.monotonic() - t0, 2)
    print(f"source read + preflight OK ({len(source_packs)} packs) in {read_seconds}s; source connection closed")

    # ---- Phase B: one short target transaction. ----
    with tgt_engine.connect() as tgt_conn:
        if neon_identity(tgt_conn) != tgt_id:  # same database as checked above
            raise TransferError("Target identity changed between checks.")
        tgt_conn.rollback()
        tgt_tx = tgt_conn.begin()
        try:
            tgt_conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            tgt_conn.execute(text("SET LOCAL lock_timeout = '15s'"))
            tgt_conn.execute(text("select pg_advisory_xact_lock(7342001)"))  # serialize concurrent runs
            tgt_session = Session(bind=tgt_conn, autoflush=False)
            report = apply_transfer(source_packs, tgt_session, SOURCE_ENDPOINT)
            report.update(mode="commit" if args.commit else "dry-run", source=src_id, target=tgt_id,
                          source_read_seconds=read_seconds)
            if report["ok"] and args.commit:
                tgt_session.flush()
                tgt_tx.commit()
                report["result"] = "COMMITTED"
            else:
                tgt_tx.rollback()
                report["result"] = "ROLLED BACK (dry run)" if report["ok"] else "ROLLED BACK (validation failed)"
        except Exception:
            if tgt_tx.is_active:
                tgt_tx.rollback()
            raise
    tgt_engine.dispose()
    report["total_seconds"] = round(time.monotonic() - t0, 2)

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scratch", "transfer_reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"transfer_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{report['mode']}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"\nRESULT: {report['result']}")
    print(f"copied: {len(report['copied'])}  skipped: {len(report['skipped'])}")
    for item in report["copied"]:
        print(f"  {item['source_quiz_id']} -> quiz {item['target_quiz_id']} / version {item['target_version_id']}  "
              f"{item['game_mode']}  rounds={item['rounds']} rq={item['round_questions']}  {item['title']}")
    for item in report["skipped"]:
        print(f"  skipped {item['source_quiz_id']} (already target quiz {item['target_quiz_id']})")
    print("counts before:", report["counts_before"])
    print("counts after: ", report["counts_after"])
    print("validation errors:", report["validation_errors"] or "none")
    print(f"timing: source {read_seconds}s, target transaction {report['target_seconds']}s, total {report['total_seconds']}s")
    print(f"report: {out_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TransferError as exc:
        print(f"REFUSED: {exc}")
        sys.exit(2)
