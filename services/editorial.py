"""
Authoritative Editorial & QA Service for ZakoWhat Curated Content.
Provides:
- QA diagnostic findings (PASS / REVIEW / REJECT, flags, audit notes, recommendations)
- Editorial decision tracking (APPROVE, REJECT, NEEDS REVIEW)
- Documented replacement candidates for defective/problematic questions
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from models import Question, AcceptedAnswer, RoundQuestion, Round, QuizVersion, Quiz
from services.official_pack_qa import MEDIA_DEPENDENCY_RE


# Documented high-quality replacement candidates from Question Bank
DOCUMENTED_REPLACEMENT_CANDIDATES: Dict[int, List[Dict[str, Any]]] = {
    # Pack 05 Q16 (ID 4163 - Piza missing visual handout)
    4163: [
        {
            "candidate_id": 37,
            "title": "Inka qurbonlik marosimi va to'tiqush",
            "reason": "Clean, authentic Zakovat cultural history question with complete explanation and verifiable accepted answers.",
            "category": "Tarix / Madaniyat",
        }
    ],
    # Pack 05 Q22 (ID 4169 - Bertillon underspecified facial traits)
    4169: [
        {
            "candidate_id": 26,
            "title": "500-so'mlik kupyuradagi me'moriy timsol (Amir Temur haykali)",
            "reason": "Authentic Uzbek cultural heritage riddle; precise deductive logic.",
            "category": "Milliy meros / San'at",
        }
    ],
    # Pack 08 Q10 (ID 4229 - Inverted wavelength physics error)
    4229: [
        {
            "candidate_id": 29,
            "title": "2010 Uimbldon afsonaviy 11 soatlik tennis bahsi (Jon Isner)",
            "reason": "Factually accurate, verifiable world sports history question.",
            "category": "Sport tarixi",
        }
    ],
}

# Cache for QA diagnostics loaded from disk
_DIAGNOSTICS_CACHE: Optional[Dict[int, Dict[str, Any]]] = None


def _load_diagnostics_cache() -> Dict[int, Dict[str, Any]]:
    global _DIAGNOSTICS_CACHE
    if _DIAGNOSTICS_CACHE is not None:
        return _DIAGNOSTICS_CACHE

    cache = {}
    path = "scratch/classified_questions_qa.json"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    qid = item.get("question_id")
                    if qid:
                        cache[qid] = item
        except Exception:
            pass

    _DIAGNOSTICS_CACHE = cache
    return cache


def get_question_qa_info(question: Question, db: Optional[Session] = None) -> Dict[str, Any]:
    """
    Returns automated QA diagnostics, audit status, flags, and candidate recommendations.
    Does NOT mutate question data.
    """
    cache = _load_diagnostics_cache()
    cached = cache.get(question.id)

    audit_status = "PASS"
    flags = []
    reasons = []
    actions = []

    if cached:
        audit_status = cached.get("status", "PASS")
        reasons = list(cached.get("reasons", []))
        actions = list(cached.get("actions", []))
    else:
        # Dynamic heuristic if not in pre-computed audit file
        text = question.text or ""
        primary_ans = ""
        accepted_texts = []
        if question.accepted_answers:
            for a in question.accepted_answers:
                accepted_texts.append(a.answer_text)
                if a.is_primary:
                    primary_ans = a.answer_text

        # 1. Missing media check
        if MEDIA_DEPENDENCY_RE.search(text):
            audit_status = "REJECT"
            flags.append({"type": "MISSING_MEDIA", "severity": "CRITICAL", "desc": "Question text contains references to missing visual/audio handout"})
            reasons.append("Matnda rasm/tarqatma materialga havola aniqlandi.")
            actions.append("Savol matnini to'liq mustaqil shaklga keltirish yoki zaxira savol bilan almashtirish tavsiya etiladi.")

        # 2. Orthography / quote checks
        if any(c in primary_ans for c in ['“', '”', '"']):
            if audit_status == "PASS":
                audit_status = "REVIEW"
            flags.append({"type": "QUOTE_MARKS", "severity": "WARNING", "desc": "Primary answer contains literal quotes"})
            reasons.append(f"Asosiy javobda qo'shtirnoq belgilari mavjud: '{primary_ans}'")
            actions.append("Qo'shtirnoqlardan tozalangan asosiy javob belgilash tavsiya etiladi.")

        if any(c in primary_ans for c in ['(', ')', '[', ']']):
            if audit_status == "PASS":
                audit_status = "REVIEW"
            flags.append({"type": "PARENTHESES", "severity": "WARNING", "desc": "Primary answer contains parentheses/brackets"})
            reasons.append(f"Asosiy javobda qavslar mavjud: '{primary_ans}'")
            actions.append("Asosiy javobni qavssiz toza holatga keltirib, variantni muqobil javob sifatida saqlash tavsiya etiladi.")

        # 3. Single answer rigidity
        if len(accepted_texts) <= 1:
            flags.append({"type": "SINGLE_ANSWER_ONLY", "severity": "INFO", "desc": f"Only 1 accepted variant: '{primary_ans}'"})
            if len(primary_ans.split()) >= 4:
                if audit_status == "PASS":
                    audit_status = "REVIEW"
                reasons.append(f"Ko'p so'zli javob ({len(primary_ans.split())} so'z) uchun faqat 1 ta muqobil mavjud.")
                actions.append("Tabiiy sinonimlar va so'z shakllarini qo'shish tavsiya etiladi.")

    # Candidate replacements
    candidates = []
    raw_candidates = DOCUMENTED_REPLACEMENT_CANDIDATES.get(question.id, [])
    if raw_candidates and db:
        for rc in raw_candidates:
            cid = rc["candidate_id"]
            cand_q = db.query(Question).filter(Question.id == cid).first()
            if cand_q:
                c_primary = next((a.answer_text for a in cand_q.accepted_answers if a.is_primary), None)
                c_alts = [a.answer_text for a in cand_q.accepted_answers if not a.is_primary]
                candidates.append({
                    "candidate_id": cand_q.id,
                    "title": rc["title"],
                    "reason": rc["reason"],
                    "category": cand_q.category or rc.get("category"),
                    "text": cand_q.text,
                    "primary_answer": c_primary,
                    "alternative_answers": c_alts,
                    "explanation": cand_q.explanation,
                })

    return {
        "audit_status": audit_status,  # PASS, REVIEW, REJECT
        "flags": flags,
        "reasons": reasons,
        "actions": actions,
        "candidate_replacements": candidates,
    }


def get_question_editorial_status(question: Question) -> str:
    """
    Returns authoritative editorial decision: 'approved', 'rejected', 'needs_review', or 'draft'.
    """
    if question.status in ("approved", "rejected", "needs_review"):
        return question.status

    # Check source_meta['editorial']
    if question.source_meta and isinstance(question.source_meta, dict):
        ed = question.source_meta.get("editorial", {})
        if isinstance(ed, dict):
            decision = str(ed.get("decision", "")).lower()
            if decision in ("approved", "approve"):
                return "approved"
            if decision in ("rejected", "reject"):
                return "rejected"
            if decision in ("needs_review", "review"):
                return "needs_review"

    # Default to question.status ('draft', 'ready', etc.)
    return question.status or "draft"


def record_editorial_decision(
    question: Question,
    decision: str,
    owner_email: str,
    notes: Optional[str] = None,
    actor_type: str = "authenticated_owner",
    executed_by: Optional[str] = None,
    authorizing_authority: Optional[str] = None,
    action_type: str = "editorial_decision",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Records an explicit Owner editorial decision on a Question.
    Allowed decisions: 'approve', 'reject', 'needs_review'.
    Updates Question.status and source_meta['editorial'].
    Maintains an immutable, append-only history in source_meta['editorial_history']
    distinguishing the authorizing authority from the execution actor.
    """
    norm = decision.strip().lower()
    if norm in ("approve", "approved"):
        target_status = "approved"
    elif norm in ("reject", "rejected"):
        target_status = "rejected"
    elif norm in ("needs_review", "review"):
        target_status = "needs_review"
    else:
        raise ValueError(f"Noto'g'ri tahririyat qarori: {decision}. Faqat 'approve', 'reject', 'needs_review' qabul qilinadi.")

    # Validation: automated scripts cannot masquerade as authenticated_owner without explicit check
    valid_actor_types = ("authenticated_owner", "automated_agent", "script_automation", "audit_reconciliation")
    if actor_type not in valid_actor_types:
        raise ValueError(f"Noto'g'ri actor_type: {actor_type}. Ruxsat etilgan: {valid_actor_types}")

    effective_exec_by = executed_by or owner_email
    effective_authority = authorizing_authority or (owner_email if actor_type == "authenticated_owner" else "Owner")

    question.status = target_status

    meta = dict(question.source_meta) if question.source_meta and isinstance(question.source_meta, dict) else {}
    editorial_data = meta.get("editorial", {}) if isinstance(meta.get("editorial"), dict) else {}
    history = list(meta.get("editorial_history", [])) if isinstance(meta.get("editorial_history"), list) else []

    now_iso = datetime.now(timezone.utc).isoformat()

    # If editorial_history is currently empty but a previous editorial record exists,
    # preserve the legacy record as the genesis history entry before appending
    if not history and editorial_data:
        history.append({
            "event_type": "legacy_baseline_record",
            "timestamp": editorial_data.get("decided_at", now_iso),
            "decision": editorial_data.get("decision", target_status),
            "actor_type": editorial_data.get("actor_type", "unspecified_legacy"),
            "executed_by": editorial_data.get("executed_by", editorial_data.get("decided_by", "unknown")),
            "authorizing_authority": editorial_data.get("authorizing_authority", editorial_data.get("decided_by")),
            "notes": editorial_data.get("notes"),
        })

    # Append new immutable audit event
    new_event = {
        "event_type": action_type,
        "timestamp": now_iso,
        "decision": target_status,
        "actor_type": actor_type,
        "executed_by": effective_exec_by,
        "authorizing_authority": effective_authority,
        "notes": notes.strip() if notes else None,
    }
    if extra_meta:
        new_event["metadata"] = extra_meta

    history.append(new_event)

    # Update active snapshot
    editorial_data.update({
        "decision": target_status,
        "decided_by": effective_authority,
        "decided_at": now_iso,
        "actor_type": actor_type,
        "executed_by": effective_exec_by,
        "authorizing_authority": effective_authority,
        "notes": notes.strip() if notes else editorial_data.get("notes"),
    })

    meta["editorial"] = editorial_data
    meta["editorial_history"] = history
    question.source_meta = meta

    return target_status


def reconcile_pack02_question_audit(
    question: Question,
    global_seq: int,
    is_alternative_question: bool,
    alternative_name: Optional[str] = None,
    executed_by: str = "release_agent",
) -> Dict[str, Any]:
    """
    Transparently reconciles the editorial audit history for a Pack 02 question.
    - Preserves existing original test1@zakowhat.uz records as historical evidence.
    - Appends transparent correction events identifying:
      1. Original automated preparation script and its incorrectly attributed identity.
      2. Owner's explicit approval of the 4 alternatives (for Q6, Q8, Q15, Q21).
      3. Owner's subsequent formal pack-level sign-off for the complete 24-question Pack 02.
      4. Actual execution actor (release agent/script) separate from the human authorizing authority.
    - Never backdates or silently overwrites history.
    """
    meta = dict(question.source_meta) if question.source_meta and isinstance(question.source_meta, dict) else {}
    editorial_data = dict(meta.get("editorial", {})) if isinstance(meta.get("editorial"), dict) else {}
    history = list(meta.get("editorial_history", [])) if isinstance(meta.get("editorial_history"), list) else []

    # 1. Establish the preserved historical baseline record if not already recorded
    if not history:
        history.append({
            "event_type": "historical_baseline_record",
            "timestamp": editorial_data.get("decided_at", "2026-09-28T16:16:52.704326+00:00"),
            "decision": editorial_data.get("decision", "approved"),
            "actor_type": "automated_script",
            "executed_by": "scratch/execute_phase1_editorial_changes.py",
            "attributed_email": editorial_data.get("decided_by", "test1@zakowhat.uz"),
            "attribution_status": "incorrectly_attributed_test_user",
            "notes": editorial_data.get("notes", f"Approved by Owner for Pack 02 release candidate"),
            "audit_disclosure": "Original record was generated by automated release preparation script; test1@zakowhat.uz was an unconfigured test account.",
        })

    now_iso = datetime.now(timezone.utc).isoformat()

    # 2. Append event for Owner's explicit approval of alternatives (if applicable)
    if is_alternative_question and alternative_name:
        history.append({
            "event_type": "owner_alternative_approval",
            "timestamp": now_iso,
            "decision": "approved",
            "actor_type": "automated_agent",
            "executed_by": executed_by,
            "authorizing_authority": "Owner",
            "scope": f"Explicit approval of alternative answer '{alternative_name}' for Q{global_seq}",
            "notes": f"Owner explicitly approved adding accepted answer alternative '{alternative_name}'. Executed by release agent.",
        })

    # 3. Append event for Owner's formal pack-level editorial sign-off for the complete 24-question pack
    history.append({
        "event_type": "owner_pack_editorial_signoff",
        "timestamp": now_iso,
        "decision": "approved",
        "actor_type": "automated_agent",
        "executed_by": executed_by,
        "authorizing_authority": "Owner",
        "scope": "Formal pack-level editorial sign-off for complete 24-question Pack 02",
        "notes": f"Owner confirmed formal pack-level editorial sign-off for Pack 02 (Q{global_seq}). Executed by release agent.",
    })

    # Update active snapshot with transparent reconciliation metadata
    editorial_data.update({
        "decision": "approved",
        "decided_by": "Owner",
        "decided_at": now_iso,
        "actor_type": "automated_agent",
        "executed_by": executed_by,
        "authorizing_authority": "Owner",
        "reconciliation_status": "reconciled_with_owner_signoff",
        "notes": f"Reconciled: formal Owner pack sign-off confirmed (Q{global_seq}). Preserved original script record in editorial_history.",
    })

    meta["editorial"] = editorial_data
    meta["editorial_history"] = history
    question.source_meta = meta
    question.status = "approved"

    return {
        "question_id": question.id,
        "global_seq": global_seq,
        "history_count": len(history),
        "latest_event": history[-1],
    }
