"""
Eski Savollar Profiling and Daily Question Extraction Pipeline

This module processes the Telegram Desktop JSON export for the 'Eski Savollar'
channel (Channel ID 2010763825), performing full source profiling, chronological
traversal, split-message resolution, multi-part handling, media dependency auditing,
duplicate detection, and conservative eligibility classification for the future
Daily Question feature.

Strict scope:
- Local extraction and analysis ONLY.
- Zero database writes, zero migrations, zero writes to PostgreSQL.
- Preserves chronological order from earliest to latest message.
"""

import os
import json
import re
from collections import Counter, defaultdict
from typing import Optional, List, Dict, Any, Tuple


# ============================================================================
# Text Cleaning & Normalization
# ============================================================================

def clean_telegram_text(text: Any) -> str:
    """
    Normalizes Telegram raw text format (strings, lists of text/dict entities)
    into a clean unified string while preserving exact characters, whitespace,
    and Unicode accents/modifiers. Strips invisible zero-width characters.
    """
    if not text:
        return ""
    if isinstance(text, str):
        return text.replace('\u200b', '').replace('\ufeff', '')
    if isinstance(text, list):
        parts = []
        for part in text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text", ""))
        return "".join(parts).replace('\u200b', '').replace('\ufeff', '')
    return str(text)


def normalize_text_for_comparison(text: str) -> str:
    """
    Normalizes text for duplicate and near-duplicate detection:
    lowercased, punctuation removed, stripped of leading question numbers/delimiters.
    """
    t = re.sub(
        r'^\s*(?:[🧠⚠️🔹🔸🔻🔺▪️▫️•\*\s]*)(?:#?\s*(?:savol|савол)\s*[:\-_#№]?\s*\d+|\b\d{1,5}\s*[\.|\)]|#savol_\d+)\s*',
        '',
        text,
        flags=re.IGNORECASE
    )
    t = t.lower()
    t = re.sub(r'[^\w\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


# ============================================================================
# Question Header & Banner Parsing
# ============================================================================

RE_HEADER = re.compile(
    r'^(?:[\s\u200b\ufeff]*[🧠⚠️🔹🔸🔻🔺▪️▫️•\*\#\d\w\s]*?(?:savol|савол|kunning\s+oxirgi\s+savoli|blits|duplet)[^\n]*\n+)?'
    r'[\s\u200b\ufeff]*(?:#?\s*(?:savol|савол)\s*[:\-_#№]?\s*(\d+)|\b(\d{1,5})\s*[\.|\)]|#savol_(\d+)|(?:savol|савол)\s*№\s*(\d+))[\.\:\-\s]*',
    re.IGNORECASE
)

RE_BANNER_LINE = re.compile(
    r'^(?:[🧠⚠️🔹🔸🔻🔺▪️▫️•\*\s]*)(kunning\s+oxirgi\s+savoli|detektiv\s+savol|blits\s+savol|'
    r'duplet|mantiqiy\s+savol|qisqa\s+savol|rebus|mualliflik\s+savol|diqqat\s+savol)[^\n]*$',
    re.IGNORECASE | re.MULTILINE
)


def parse_question_header(text: str) -> Tuple[Optional[int], Optional[str], Optional[str], str]:
    """
    Extracts question numbering, genre banner, raw header, and cleaned question body.
    Returns: (question_number, genre_banner, raw_header, clean_question_body)
    """
    genre_banner = None
    m_banner = RE_BANNER_LINE.search(text[:200])
    if m_banner:
        genre_banner = m_banner.group(0).strip()

    m_head = RE_HEADER.match(text)
    if m_head:
        groups = [g for g in m_head.groups() if g is not None]
        q_num = int(groups[0]) if groups else None
        raw_header = m_head.group(0).strip()
        body = text[m_head.end():].strip()
    else:
        m_fallback = re.match(r'^\s*(\d{1,5})\s*[\.|\)]\s*', text)
        if m_fallback:
            q_num = int(m_fallback.group(1))
            raw_header = m_fallback.group(0).strip()
            body = text[m_fallback.end():].strip()
        else:
            q_num = None
            raw_header = None
            body = text.strip()

    # Clean body: if leading line in body is a banner that was matched, remove it
    lines = body.split('\n')
    if lines and RE_BANNER_LINE.match(lines[0].strip()):
        if not genre_banner:
            genre_banner = lines[0].strip()
        body = '\n'.join(lines[1:]).strip()

    return q_num, genre_banner, raw_header, body


# ============================================================================
# Answer Delimiter & Component Extraction
# ============================================================================

RE_ANS_SPLIT = re.compile(
    r'(?:^|\n)\s*(?:[✅✔️▪️▫️•\*\s]*)(?:javob(?:lar|i)?|жавоб(?:лар|и)?|j\b)\s*[:\.\-]?\s*',
    re.IGNORECASE
)

RE_IZOH_SPLIT = re.compile(
    r'\n\s*(?:izoh|изоҳ|izox|sharh|eslatma|xulosa)\s*[:\.\-]?\s*',
    re.IGNORECASE
)

RE_QABUL_SPLIT = re.compile(
    r'\n\s*(?:qabul(?:\s*qilish\s*mumkin|\s*qilinadi)?|hisob|ҳисоб)\s*[:\.\-]?\s*',
    re.IGNORECASE
)

RE_SOURCE_SPLIT = re.compile(
    r'\n\s*(?:manba|манба|iqtibos)\s*[:\.\-]?\s*',
    re.IGNORECASE
)

RE_AUTHOR_SPLIT = re.compile(
    r'\n\s*(?:muallif|муаллиф|savol\s+muallifi)\s*[:\.\-]?\s*',
    re.IGNORECASE
)


def parse_answer_components(
    ans_block: str
) -> Tuple[str, List[str], Optional[str], Optional[str], Optional[str], List[str]]:
    """
    Parses an answer block into:
    (primary_answer, accepted_answers, explanation, source, author, warnings).
    Handles 'Izoh:', 'Qabul:', 'Manba:', 'Muallif:', and parenthetical alternatives.
    """
    warnings = []
    text = ans_block.strip()
    explanation = None
    source = None
    author = None
    accepted = []

    # 1. Extract Author if present
    auth_m = RE_AUTHOR_SPLIT.split(text, maxsplit=1)
    if len(auth_m) == 2:
        text, author = auth_m[0].strip(), auth_m[1].strip()

    # 2. Extract Source if present
    src_m = RE_SOURCE_SPLIT.split(text, maxsplit=1)
    if len(src_m) == 2:
        text, source = src_m[0].strip(), src_m[1].strip()

    # 3. Extract Explanation (Izoh) if present
    izoh_m = RE_IZOH_SPLIT.split(text, maxsplit=1)
    if len(izoh_m) == 2:
        text, explanation_rest = izoh_m[0].strip(), izoh_m[1].strip()
        auth_nested = RE_AUTHOR_SPLIT.split(explanation_rest, maxsplit=1)
        if len(auth_nested) == 2:
            explanation_rest, author = auth_nested[0].strip(), auth_nested[1].strip()
        src_nested = RE_SOURCE_SPLIT.split(explanation_rest, maxsplit=1)
        if len(src_nested) == 2:
            explanation_rest, source = src_nested[0].strip(), src_nested[1].strip()
        explanation = explanation_rest.strip()

    # 4. Extract Accepted alternatives (Qabul) if present
    qabul_m = RE_QABUL_SPLIT.split(text, maxsplit=1)
    if len(qabul_m) == 2:
        text, qabul_text = qabul_m[0].strip(), qabul_m[1].strip()
        for item in re.split(r'[,;\n]+', qabul_text):
            item_s = item.strip().rstrip('.')
            if item_s and item_s not in accepted:
                accepted.append(item_s)

    # 5. Process Primary Answer and check for inline parentheses alternatives
    primary = text.strip()
    paren_m = re.match(r'^(.*?)\s*\((.*?)\)\s*(.*?)$', primary)
    if paren_m:
        before = paren_m.group(1).strip()
        inside = paren_m.group(2).strip()
        after = paren_m.group(3).strip()
        if before and len(inside) > 1 and not re.search(r'\b(?:chunki|sababi|ya\'ni|izoh)\b', inside, re.IGNORECASE):
            q_clean = re.sub(r'\s*(?:ham\s+)?qabul.*$', '', inside, flags=re.IGNORECASE).strip()
            if q_clean and q_clean not in accepted:
                accepted.append(q_clean)
            if not after:
                primary = before

    primary = primary.rstrip('.').strip()

    if not primary:
        warnings.append("empty_primary_answer")
    elif len(primary) > 200:
        warnings.append("unusually_long_primary_answer")

    return primary, accepted, explanation, source, author, warnings


def split_question_and_answer(
    text: str
) -> Tuple[str, Optional[str], List[str], Optional[str], Optional[str], Optional[str], List[str]]:
    """
    Splits a single text into question body and answer components.
    Returns: (question_part, primary_answer, accepted_answers, explanation, source, author, warnings)
    If text begins with an answer delimiter (standalone answer), question_part will be empty string.
    """
    parts = RE_ANS_SPLIT.split(text, maxsplit=1)
    if len(parts) == 2:
        q_part = parts[0].strip()
        ans_part = parts[1].strip()
        if ans_part:
            pa, acc, exp, src, auth, warns = parse_answer_components(ans_part)
            return q_part, pa, acc, exp, src, auth, warns
    return text.strip(), None, [], None, None, None, []


# ============================================================================
# Media Dependency & Multi-part Handling
# ============================================================================

# Strict visual dependency when message HAS attachment
RE_VISUAL_WITH_ATTACHMENT = re.compile(
    r'(?:'
    r'(?:ushbu|bu|mazkur|quyidagi|yuqoridagi)\s+(?:rasm|surat|tasvir|karikatura|diagramma|foto)'
    r'|\b(?:rasmda|rasmdagi\w*|rasmga\s+qarab|suratda|suratdagi\w*|suratga\s+qarab|karikaturada|diagrammada|foto(?:surat)?da)\b'
    r'|\b(?:surat|rasm)da\s+(?:nima|kim|qaysi|koʻrib|ko\'rib)'
    r'|\b(?:rasm|surat)ga\s+diqqat\s+qiling\b'
    r'|\b(?:qarshingizda\s+(?:siz\s+)?(?:rasm|surat|plakat|karikatura))\b'
    r')',
    re.IGNORECASE
)

# Strict visual dependency when message has NO attachment
RE_DIRECT_VISUAL_INSTRUCTION = re.compile(
    r'(?:'
    r'\b(?:suratga|rasmga)\s+qarab\b'
    r'|(?:ushbu|bu|mazkur|quyidagi|yuqoridagi)\s+(?:rasm|surat|tasvir|karikatura|diagramma|foto)(?:da|dagi\w*|ga|\b)'
    r'|\b(?:rasmdagi|suratdagi|diagrammadagi)\w*\b'
    r'|\b(?:surat|rasm)da\s+(?:nima|kim|qaysi|koʻrib|ko\'rib)'
    r'|\b(?:rasm|surat)ga\s+diqqat\s+qiling\b'
    r'|\b(?:qarshingizda\s+(?:siz\s+)?(?:rasm|surat|plakat|karikatura))\b'
    r')',
    re.IGNORECASE
)

RE_MULTIPART_KEYWORD = re.compile(r'\b(?:duplet|blits|триплет|triplet)\b', re.IGNORECASE)
RE_SUBQUESTIONS = re.compile(r'(?:^|\n)\s*(?:[1-5][⃣\.\)]|[A-Da-d][\.\)])\s+(.+)', re.MULTILINE)

RE_NOISE = re.compile(
    r'(?:yakuniy\s+natijalar|bugungi\s+natijalar|natijalar\s*:\s*\n|kanaldagi\s+savollar\s+soni|'
    r'savollar\s+soni\s+\d+\s+ta|reklama\s+qilishda\s+davom|obuna\s+bo[\'ʻ]ling|'
    r'admin\s*:\s*@|savol\s+yuborish\s+uchun\s*:\s*@|qoidalar\s+yoki\s+eslatmalar|'
    r'ovozli\s+turnir|turnir\s+yakunlandi|eng\s+faol\s+bilimdon|iq_matematika|'
    r'shartlar\s*:\s*kanallarga\s+obuna|pul\s+yutug[\'ʻ]i)',
    re.IGNORECASE
)


def detect_media_dependency(
    text: str,
    msg: Dict[str, Any],
    local_media_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Audits media presence in message metadata and text.
    Checks whether visual context is required and whether referenced file exists locally.
    """
    has_photo = "photo" in msg or ("file" in msg and msg.get("media_type") == "photo")
    has_file = "file" in msg and not has_photo
    media_type = msg.get("media_type") or ("photo" if has_photo else ("file" if has_file else None))
    file_id = msg.get("photo") or msg.get("file")

    if has_photo or has_file:
        is_essential_visual = bool(RE_VISUAL_WITH_ATTACHMENT.search(text))
    else:
        is_essential_visual = bool(RE_DIRECT_VISUAL_INSTRUCTION.search(text))

    local_exists = False
    if file_id and local_media_dir:
        candidate_path = os.path.join(local_media_dir, str(file_id))
        if os.path.exists(candidate_path):
            local_exists = True

    return {
        "has_media_attachment": has_photo or has_file,
        "media_type": media_type,
        "file_id": file_id,
        "is_essential_visual": is_essential_visual,
        "local_media_present": local_exists,
    }


def detect_multipart(text: str) -> Tuple[bool, List[str]]:
    """
    Detects whether the question is a multi-part challenge (duplet, triplet, blits, or enumerated).
    """
    has_keyword = bool(RE_MULTIPART_KEYWORD.search(text))
    sub_matches = RE_SUBQUESTIONS.findall(text)
    is_multi = has_keyword or (len(sub_matches) >= 2)
    return is_multi, sub_matches


# ============================================================================
# Main Extraction Pipeline
# ============================================================================

def extract_eski_savollar(
    input_path: str,
    output_dir: str,
    local_media_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extracts, profiles, and classifies all messages from the Eski Savollar Telegram export.
    Outputs:
    - eski_savollar_questions.json
    - eski_savollar_profiling_report.json
    - eski_savollar_media_inventory.json
    - eski_savollar_duplicates_report.json
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    os.makedirs(output_dir, exist_ok=True)

    with open(input_path, "r", encoding="utf-8") as f:
        export_data = json.load(f)

    channel_name_raw = export_data.get("name", "Eski Savollar")
    channel_name = "Eski Savollar"
    channel_id = export_data.get("id", 2010763825)
    channel_type = export_data.get("type", "public_channel")
    messages = export_data.get("messages", [])

    total_messages = len(messages)
    service_messages = 0
    regular_messages = 0
    earliest_date = None
    latest_date = None

    # Content counts
    with_text = 0
    empty_text = 0
    with_photo = 0
    with_file = 0
    media_breakdown = Counter()
    has_reply_count = 0

    # Map replies and messages by ID
    reply_map = defaultdict(list)
    msg_map = {}
    for m in messages:
        mid = m.get("id")
        msg_map[mid] = m
        if m.get("reply_to_message_id"):
            reply_map[m["reply_to_message_id"]].append(m)

    extracted_candidates = []
    media_inventory_items = []
    seen_norm_questions = {}
    duplicate_groups = defaultdict(list)

    re_q_num = re.compile(
        r'(?:^|\n)\s*(?:[🧠⚠️🔹🔸🔻🔺▪️▫️•\*\s]*)(?:#?\s*(?:savol|савол)\s*[:\-_#№]?\s*(\d+)|\b(\d{1,5})\s*[\.|\)]|#savol_(\d+)|(?:savol|савол)\s*№\s*(\d+))',
        re.IGNORECASE
    )

    for idx, m in enumerate(messages):
        m_id = m.get("id")
        m_type = m.get("type")
        m_date = m.get("date")

        if m_date:
            if earliest_date is None or m_date < earliest_date:
                earliest_date = m_date
            if latest_date is None or m_date > latest_date:
                latest_date = m_date

        if m_type == "service":
            service_messages += 1
            extracted_candidates.append({
                "question_id": f"eski_{channel_id}_{m_id}",
                "channel_id": channel_id,
                "message_id": m_id,
                "date": m_date,
                "raw_text": "",
                "clean_text": "",
                "question_number": None,
                "genre_or_banner": None,
                "primary_answer": "",
                "accepted_answers": [],
                "explanation": None,
                "source": None,
                "author": None,
                "is_multipart": False,
                "multipart_details": [],
                "has_media_attachment": False,
                "media_type": None,
                "media_file_id": None,
                "is_essential_visual": False,
                "local_media_present": False,
                "resolution_method": "none",
                "reply_to_message_id": None,
                "classification": "excluded_non_question",
                "classification_reasons": ["service_message"]
            })
            continue

        regular_messages += 1
        raw_txt = clean_telegram_text(m.get("text", "")).strip()

        # Media inventory check
        has_p = "photo" in m or ("file" in m and m.get("media_type") == "photo")
        has_f = "file" in m and not has_p
        if has_p:
            with_photo += 1
            media_breakdown["photo"] += 1
        if has_f:
            with_file += 1
            media_breakdown["file"] += 1
        if m.get("media_type") and m["media_type"] not in ("photo", "file"):
            media_breakdown[m["media_type"]] += 1

        if m.get("reply_to_message_id"):
            has_reply_count += 1

        if has_p or has_f:
            m_info = detect_media_dependency(raw_txt, m, local_media_dir)
            media_inventory_items.append({
                "message_id": m_id,
                "date": m_date,
                "media_type": m_info["media_type"],
                "file_id": m_info["file_id"],
                "has_text": bool(raw_txt),
                "text_snippet": raw_txt[:120] if raw_txt else "",
                "text_references_image": m_info["is_essential_visual"],
                "local_file_exists": m_info["local_media_present"]
            })

        if not raw_txt:
            empty_text += 1
            extracted_candidates.append({
                "question_id": f"eski_{channel_id}_{m_id}",
                "channel_id": channel_id,
                "message_id": m_id,
                "date": m_date,
                "raw_text": "",
                "clean_text": "",
                "question_number": None,
                "genre_or_banner": None,
                "primary_answer": "",
                "accepted_answers": [],
                "explanation": None,
                "source": None,
                "author": None,
                "is_multipart": False,
                "multipart_details": [],
                "has_media_attachment": has_p or has_f,
                "media_type": m.get("media_type"),
                "media_file_id": m.get("photo") or m.get("file"),
                "is_essential_visual": False,
                "local_media_present": False,
                "resolution_method": "none",
                "reply_to_message_id": m.get("reply_to_message_id"),
                "classification": "excluded_non_question",
                "classification_reasons": ["empty_message"]
            })
            continue

        with_text += 1

        # Check noise patterns (ads, announcements, leaderboards)
        if RE_NOISE.search(raw_txt):
            extracted_candidates.append({
                "question_id": f"eski_{channel_id}_{m_id}",
                "channel_id": channel_id,
                "message_id": m_id,
                "date": m_date,
                "raw_text": raw_txt,
                "clean_text": raw_txt,
                "question_number": None,
                "genre_or_banner": None,
                "primary_answer": "",
                "accepted_answers": [],
                "explanation": None,
                "source": None,
                "author": None,
                "is_multipart": False,
                "multipart_details": [],
                "has_media_attachment": has_p or has_f,
                "media_type": m.get("media_type"),
                "media_file_id": m.get("photo") or m.get("file"),
                "is_essential_visual": False,
                "local_media_present": False,
                "resolution_method": "none",
                "reply_to_message_id": m.get("reply_to_message_id"),
                "classification": "excluded_non_question",
                "classification_reasons": ["admin_announcement_or_results"]
            })
            continue

        # Try inline split
        q_raw, pa, acc, exp, src, auth, warns = split_question_and_answer(raw_txt)
        res_method = "none"

        if q_raw and pa:
            res_method = "inline"
        elif not q_raw and pa:
            # Standalone answer fragment without preceding question
            extracted_candidates.append({
                "question_id": f"eski_{channel_id}_{m_id}",
                "channel_id": channel_id,
                "message_id": m_id,
                "date": m_date,
                "raw_text": raw_txt,
                "clean_text": "",
                "question_number": None,
                "genre_or_banner": None,
                "primary_answer": pa,
                "accepted_answers": acc,
                "explanation": exp,
                "source": src,
                "author": auth,
                "is_multipart": False,
                "multipart_details": [],
                "has_media_attachment": has_p or has_f,
                "media_type": m.get("media_type"),
                "media_file_id": m.get("photo") or m.get("file"),
                "is_essential_visual": False,
                "local_media_present": False,
                "resolution_method": "standalone_answer",
                "reply_to_message_id": m.get("reply_to_message_id"),
                "classification": "excluded_non_question",
                "classification_reasons": ["standalone_answer_fragment"]
            })
            continue
        else:
            # Check if this message is a question awaiting an answer
            m_q = re_q_num.search(raw_txt)
            if m_q or ("savol" in raw_txt.lower() and len(raw_txt) > 35):
                # 1. Try finding answer in replies
                if m_id in reply_map:
                    for reply_m in reply_map[m_id]:
                        rep_txt = clean_telegram_text(reply_m.get("text", "")).strip()
                        _, rep_pa, rep_acc, rep_exp, rep_src, rep_auth, rep_warns = split_question_and_answer(rep_txt)
                        if rep_pa:
                            pa, acc, exp, src, auth, warns = rep_pa, rep_acc, rep_exp, rep_src, rep_auth, rep_warns
                            res_method = "reply_to"
                            break

                # 2. Try finding answer in immediate next message
                if not pa and idx + 1 < len(messages):
                    next_m = messages[idx + 1]
                    next_txt = clean_telegram_text(next_m.get("text", "")).strip()
                    _, nxt_pa, nxt_acc, nxt_exp, nxt_src, nxt_auth, nxt_warns = split_question_and_answer(next_txt)
                    if nxt_pa:
                        m_next_q = re_q_num.search(next_txt)
                        if not m_next_q or (m_q and m_next_q and m_q.group(0).strip() == m_next_q.group(0).strip()):
                            pa, acc, exp, src, auth, warns = nxt_pa, nxt_acc, nxt_exp, nxt_src, nxt_auth, nxt_warns
                            res_method = "sequential_next"

        # Parse question header and body
        q_num, genre_banner, raw_header, clean_q_body = parse_question_header(q_raw)

        # Detect media dependencies
        media_info = detect_media_dependency(clean_q_body, m, local_media_dir)

        # Detect multipart
        is_multi, subquestions = detect_multipart(clean_q_body)

        # Classify candidate
        classification = "eligible_candidate"
        reasons = []

        if len(clean_q_body) < 18:
            classification = "excluded_non_question"
            reasons.append("insufficient_text")
        elif not pa:
            classification = "blocked_missing_answer"
            reasons.append("question_without_answer")
        elif media_info["is_essential_visual"] and not media_info["local_media_present"]:
            classification = "blocked_missing_media"
            reasons.append("essential_visual_reference_missing")
        elif media_info["has_media_attachment"] and not media_info["local_media_present"]:
            classification = "needs_review"
            reasons.append("photo_attached_needs_verification")
        elif is_multi:
            classification = "needs_review"
            reasons.append("multi_part_question")
        elif warns:
            classification = "needs_review"
            reasons.extend(warns)
        else:
            # Check for duplicate
            norm_key = normalize_text_for_comparison(clean_q_body)
            if len(norm_key) > 35:
                if norm_key in seen_norm_questions:
                    classification = "needs_review"
                    earlier_id = seen_norm_questions[norm_key]
                    reasons.append(f"suspected_duplicate_of_msg_{earlier_id}")
                    duplicate_groups[norm_key].append(m_id)
                else:
                    seen_norm_questions[norm_key] = m_id
                    reasons.append("clean_self_contained_qa")
            else:
                reasons.append("clean_self_contained_qa")

        extracted_candidates.append({
            "question_id": f"eski_{channel_id}_{m_id}",
            "channel_id": channel_id,
            "message_id": m_id,
            "date": m_date,
            "question_number": q_num,
            "genre_or_banner": genre_banner,
            "raw_text": raw_txt,
            "clean_text": clean_q_body,
            "primary_answer": pa or "",
            "accepted_answers": acc,
            "explanation": exp,
            "source": src,
            "author": auth,
            "is_multipart": is_multi,
            "multipart_details": subquestions,
            "has_media_attachment": media_info["has_media_attachment"],
            "media_type": media_info["media_type"],
            "media_file_id": media_info["file_id"],
            "is_essential_visual": media_info["is_essential_visual"],
            "local_media_present": media_info["local_media_present"],
            "resolution_method": res_method,
            "reply_to_message_id": m.get("reply_to_message_id"),
            "classification": classification,
            "classification_reasons": reasons
        })

    # 2. Build Duplicates Report
    dup_report_groups = []
    total_flagged_dups = 0
    for norm_key, dup_ids in duplicate_groups.items():
        original_id = seen_norm_questions[norm_key]
        all_ids = [original_id] + dup_ids
        total_flagged_dups += len(dup_ids)
        dup_report_groups.append({
            "normalized_key": norm_key[:80],
            "total_occurrences": len(all_ids),
            "original_message_id": original_id,
            "duplicate_message_ids": dup_ids
        })

    duplicates_report = {
        "channel_id": channel_id,
        "total_duplicate_groups": len(dup_report_groups),
        "total_duplicate_messages_flagged": total_flagged_dups,
        "duplicate_groups": dup_report_groups
    }

    # 3. Build Classification Census
    classification_counts = Counter(c["classification"] for c in extracted_candidates)

    q_nums = [c["question_number"] for c in extracted_candidates if c["question_number"] is not None]
    min_q = min(q_nums) if q_nums else None
    max_q = max(q_nums) if q_nums else None

    profiling_report = {
        "channel_name": channel_name,
        "channel_name_raw": channel_name_raw,
        "channel_id": channel_id,
        "channel_type": channel_type,
        "total_messages": total_messages,
        "service_messages": service_messages,
        "regular_messages": regular_messages,
        "date_range": {
            "earliest": earliest_date,
            "latest": latest_date
        },
        "content_inventory": {
            "with_text": with_text,
            "empty_text": empty_text,
            "with_photo": with_photo,
            "with_file": with_file,
            "media_breakdown": dict(media_breakdown),
            "with_replies": has_reply_count
        },
        "question_numbering_census": {
            "total_with_question_number": len(q_nums),
            "min_question_number": min_q,
            "max_question_number": max_q
        },
        "classification_breakdown": dict(classification_counts),
        "media_audit": {
            "total_media_messages": len(media_inventory_items),
            "local_media_present_count": 0,
            "local_media_status": "absent"
        },
        "duplicates_summary": {
            "total_duplicate_groups": len(dup_report_groups),
            "total_duplicate_questions": total_flagged_dups
        }
    }

    media_inventory = {
        "total_media_messages": len(media_inventory_items),
        "local_files_available": 0,
        "local_files_missing": len(media_inventory_items),
        "media_items": media_inventory_items
    }

    # 4. Save JSON files to output directory
    questions_file = os.path.join(output_dir, "eski_savollar_questions.json")
    report_file = os.path.join(output_dir, "eski_savollar_profiling_report.json")
    media_file = os.path.join(output_dir, "eski_savollar_media_inventory.json")
    dups_file = os.path.join(output_dir, "eski_savollar_duplicates_report.json")

    with open(questions_file, "w", encoding="utf-8") as f:
        json.dump(extracted_candidates, f, ensure_ascii=False, indent=2)

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(profiling_report, f, ensure_ascii=False, indent=2)

    with open(media_file, "w", encoding="utf-8") as f:
        json.dump(media_inventory, f, ensure_ascii=False, indent=2)

    with open(dups_file, "w", encoding="utf-8") as f:
        json.dump(duplicates_report, f, ensure_ascii=False, indent=2)

    return {
        "total_messages": total_messages,
        "classifications": dict(classification_counts),
        "questions_saved": len(extracted_candidates),
        "output_files": [questions_file, report_file, media_file, dups_file]
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        from services.raw_sources import raw_export_path
    except ImportError:  # run as a plain script from inside services/
        from raw_sources import raw_export_path
    src = raw_export_path("result.json")  # local-only raw export (data/raw/telegram/)
    dst = os.path.join("data", "eski_savollar_extracted")
    print(f"Running Eski Savollar extraction: {src} -> {dst}...")
    res = extract_eski_savollar(src, dst)
    print("Done! Summary:")
    print(json.dumps(res, indent=2))
