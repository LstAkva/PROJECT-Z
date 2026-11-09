import os
import json
import re
import hashlib
from typing import Optional, List, Dict, Any, Tuple


def clean_text(text: Any) -> str:
    """
    Normalizes Telegram raw text format (strings or list of text/dict entities)
    into a unified string while preserving exact characters, whitespace, and Unicode.
    """
    if not text:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(text)


def parse_theme(preamble: str) -> Tuple[Optional[str], Optional[str], str, bool, Optional[str]]:
    """
    Extracts theme components from the text preceding the first question:
    Returns (theme_prefix, theme_title, theme_raw, is_matrix_theme, matrix_clue).
    Preserves exact raw text while identifying clean title and matrix indicators.
    """
    theme_raw = preamble.strip()
    if not theme_raw:
        return None, None, "", False, None

    lines = [l.strip() for l in theme_raw.split("\n") if l.strip()]
    if not lines:
        return None, None, theme_raw, False, None

    first_line = lines[0]
    theme_prefix = None
    theme_title = first_line

    # 1. Match prefix variations: '1-mavzu:', 'Mavzu:', 'MAVZU -', '12-mavzu.', etc.
    m_pref = re.match(r"^(?:(\d+[-\s.]*)?mavzu\s*[:\.\-]?\s*)(.*)", first_line, re.IGNORECASE)
    if m_pref:
        p_num = (m_pref.group(1) or "").strip()
        theme_prefix = f"{p_num}mavzu".strip("-.") if p_num else "mavzu"
        remainder = m_pref.group(2).strip()
        if remainder:
            theme_title = remainder
        elif len(lines) > 1:
            theme_title = lines[1]
    else:
        # Check for unnumbered theme headers without 'mavzu', e.g. "Yunon mifologiyasi."
        theme_title = first_line.rstrip(".")

    # 2. Detect matrix theme patterns (where theme is an essential constraint/clue)
    # Examples: 'Matritsali -SKA-', 'Matritsali OK', 'Initsial AN', 'Javobda faqat "A" unlisi'
    is_matrix = bool(re.search(r"matrits|matritsa|initsial|\bharf\b|\bunli\b", theme_raw, re.IGNORECASE))

    matrix_clue = None
    if is_matrix:
        # Check for explicit token clue, e.g. -SKA-, OK, "A", AN
        m_dash = re.search(r"-([A-Za-z\u0400-\u04FF]+)-", theme_title)
        m_quote = re.search(r'["\']([A-Za-z\u0400-\u04FF]+)["\']', theme_title)
        m_initial = re.search(r"(?:initsial|matritsali|harf(?:i|idan)?)\s*[:\-]?\s*([A-Za-z\u0400-\u04FF\s]{1,6})", theme_title, re.IGNORECASE)
        if m_dash:
            matrix_clue = f"-{m_dash.group(1)}-"
        elif m_quote:
            matrix_clue = m_quote.group(1)
        elif m_initial:
            matrix_clue = m_initial.group(1).strip()
        else:
            matrix_clue = theme_title

    return theme_prefix, theme_title, theme_raw, is_matrix, matrix_clue


def parse_answer_and_explanation(
    qa_block: str
) -> Tuple[str, Optional[str], List[Dict[str, str]], Optional[str], List[str]]:
    """
    Parses a single question block into:
    (question_text, primary_answer, accepted_answers, explanation, warnings).
    Handles Latin and Cyrillic delimiters, 'Qabul:' alternatives, and 'Izoh:' explanations.
    Does NOT invent answers if delimiter is missing.
    """
    warnings = []
    qa_block = qa_block.strip()

    # 1. Split explanation (Izoh / Изоҳ / Примечание)
    izoh_split = re.split(
        r"\n\s*(?:izoh|изоҳ|izox|примечание)\s*[:\-\.]\s*",
        qa_block,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    explanation = None
    if len(izoh_split) > 1:
        explanation = izoh_split[1].strip()
        body = izoh_split[0].strip()
    else:
        body = qa_block

    # 2. Split question text and answer
    # Supported prefixes: J:, Javob:, Javoblar:, Жавоб:, Ж:, Ответ:, etc.
    ans_split = re.split(
        r"(?:\n|\b)(?:javob|жавоб|j|ж|ответ)\s*[:\-\.]\s*",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )

    if len(ans_split) < 2:
        # No standard answer delimiter found
        warnings.append("missing_answer_delimiter")
        return body, None, [], explanation, warnings

    question_text = ans_split[0].strip()
    raw_answer = ans_split[1].strip()

    if not raw_answer:
        warnings.append("empty_answer_text")
        return question_text, None, [], explanation, warnings

    # 3. Parse accepted alternatives (Qabul / Қабул / Зачет / Zachot)
    qabul_parts = re.split(
        r"\b(?:qabul|қабул|зачет|zachot)\s*[:\-\.]\s*",
        raw_answer,
        flags=re.IGNORECASE,
    )

    primary_answer = qabul_parts[0].strip()
    alt_answers = []
    if len(qabul_parts) > 1:
        for p in qabul_parts[1:]:
            cand = p.strip().rstrip(".)")
            if cand:
                alt_answers.append(cand)

    # Clean trailing bracket artifact if answer ended in '(' before Qabul
    if primary_answer.endswith("(") and alt_answers:
        primary_answer = primary_answer[:-1].strip()

    accepted_list = [{"text": primary_answer, "type": "primary"}]
    for alt in alt_answers:
        if alt.lower() != primary_answer.lower():
            accepted_list.append({"text": alt, "type": "zachot"})

    return question_text, primary_answer, accepted_list, explanation, warnings


def generate_stable_id(channel_id: Any, message_id: int, points: int, position: int) -> str:
    """
    Generates a deterministic, reproducible unique hash ID for each question.
    """
    raw_key = f"svoyak_{channel_id}_{message_id}_{points}_{position}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]


def extract_svoyak_groups_from_message(
    msg: Dict[str, Any],
    channel_id: str,
    channel_name: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Extracts a five-question theme group from a single Telegram message.
    Returns None if the message contains no point progression.
    """
    msg_id = msg.get("id")
    raw_text = clean_text(msg.get("text", ""))
    if not raw_text or len(raw_text.strip()) < 10:
        return None

    # Detect Svoya Igra point markers (10, 20, 30, 40, 50)
    matches = list(re.finditer(r"(?:^|\n)(?:(?:\d+\.)?(10|20|30|40|50)[\.\)]\s+)", raw_text))
    if not matches:
        return None

    # Extract preamble before first question marker
    preamble = raw_text[:matches[0].start()].strip()
    theme_prefix, theme_title, theme_raw, is_matrix, matrix_clue = parse_theme(preamble)

    group_id = f"svoyak_{channel_id}_{msg_id}"
    extracted_questions = []
    group_warnings = []

    points_sequence = [int(m.group(1)) for m in matches]
    is_standard_5 = (points_sequence == [10, 20, 30, 40, 50])

    if is_standard_5:
        group_status = "complete"
    elif len(points_sequence) < 5:
        group_status = "incomplete"
        group_warnings.append(f"fewer_than_5_questions: found {len(points_sequence)} ({points_sequence})")
    else:
        group_status = "irregular"
        group_warnings.append(f"irregular_points_sequence: found {len(points_sequence)} ({points_sequence})")

    # Extract each question block
    for idx, match in enumerate(matches):
        pts = int(match.group(1))
        pos = idx + 1
        start_idx = match.end()
        end_idx = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_text)
        block = raw_text[start_idx:end_idx].strip()
        raw_block_with_marker = raw_text[match.start():end_idx].strip()

        q_text, primary_ans, accepted_list, explanation, q_warnings = parse_answer_and_explanation(block)

        q_id = generate_stable_id(channel_id, msg_id, pts, pos)

        has_answer = bool(primary_ans and primary_ans.strip())
        editorial_flags = []
        if not has_answer:
            editorial_flags.append("missing_answer")
        if is_matrix:
            editorial_flags.append("matrix_theme")
        if not is_standard_5:
            editorial_flags.append(f"non_standard_group_{group_status}")

        editorial_status = "ready" if has_answer else "needs_review"

        q_record = {
            "id": q_id,
            "group_id": group_id,
            "position": pos,
            "points": pts,
            "category": theme_title or "Umumiy",
            "theme_prefix": theme_prefix,
            "theme_title": theme_title,
            "theme_raw": theme_raw,
            "text": q_text,
            "raw_block": raw_block_with_marker,
            "primary_answer": primary_ans,
            "accepted_answers": accepted_list,
            "explanation": explanation,
            "round_type": "svoyak_theme",
            "is_matrix_theme": is_matrix,
            "matrix_clue": matrix_clue,
            "has_answer": has_answer,
            "source": {
                "source_name": "Svoya Igra baza",
                "channel_id": str(channel_id),
                "channel_name": channel_name,
                "question_message_id": msg_id,
                "message_date": msg.get("date"),
                "media": {
                    "has_media": bool(msg.get("photo") or msg.get("file")),
                    "file_name": msg.get("file_name"),
                    "mime_type": msg.get("mime_type"),
                } if (msg.get("photo") or msg.get("file")) else None,
            },
            "editorial": {
                "status": editorial_status,
                "flags": editorial_flags,
                "warnings": q_warnings,
            },
            "warnings": q_warnings,
        }
        extracted_questions.append(q_record)

    group_record = {
        "group_id": group_id,
        "channel_id": str(channel_id),
        "channel_name": channel_name,
        "message_id": msg_id,
        "message_date": msg.get("date"),
        "raw_message_text": raw_text,
        "theme_prefix": theme_prefix,
        "theme_title": theme_title,
        "theme_raw": theme_raw,
        "is_matrix_theme": is_matrix,
        "matrix_clue": matrix_clue,
        "group_status": group_status,
        "points_sequence": points_sequence,
        "questions_count": len(extracted_questions),
        "questions": extracted_questions,
        "warnings": group_warnings,
    }
    return group_record


def extract_svoyak_dataset(
    json_path: str,
    output_dir: str = "data/svoyak_extracted"
) -> Dict[str, Any]:
    """
    Processes a raw Telegram export JSON, extracts preserved five-question theme groups
    and individual question records, and writes new output files in output_dir.
    Does NOT overwrite any existing canonical or original JSON files.
    """
    os.makedirs(output_dir, exist_ok=True)
    basename = os.path.basename(json_path)
    prefix = os.path.splitext(basename)[0]

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    channel_id = str(data.get("id"))
    channel_name = data.get("name")
    messages = data.get("messages", [])

    total_messages = len(messages)
    theme_groups = []
    flat_questions = []

    stats = {
        "source_file": basename,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "total_source_messages": total_messages,
        "messages_with_themes": 0,
        "complete_5_question_groups": 0,
        "incomplete_groups": 0,
        "irregular_groups": 0,
        "matrix_theme_groups": 0,
        "total_extracted_questions": 0,
        "questions_by_tier": {10: 0, 20: 0, 30: 0, 40: 0, 50: 0, "other": 0},
        "questions_with_answers": 0,
        "questions_missing_answers": 0,
        "duplicate_question_ids": 0,
        "duplicate_source_message_refs": 0,
    }

    seen_q_ids = set()
    seen_msg_ids = set()

    for msg in messages:
        group = extract_svoyak_groups_from_message(msg, channel_id, channel_name)
        if not group:
            continue

        mid = msg.get("id")
        if mid in seen_msg_ids:
            stats["duplicate_source_message_refs"] += 1
        seen_msg_ids.add(mid)

        if group["theme_title"]:
            stats["messages_with_themes"] += 1

        if group["group_status"] == "complete":
            stats["complete_5_question_groups"] += 1
        elif group["group_status"] == "incomplete":
            stats["incomplete_groups"] += 1
        else:
            stats["irregular_groups"] += 1

        if group["is_matrix_theme"]:
            stats["matrix_theme_groups"] += 1

        theme_groups.append(group)

        for q in group["questions"]:
            stats["total_extracted_questions"] += 1
            pts = q["points"]
            if pts in stats["questions_by_tier"]:
                stats["questions_by_tier"][pts] += 1
            else:
                stats["questions_by_tier"]["other"] += 1

            if q["has_answer"]:
                stats["questions_with_answers"] += 1
            else:
                stats["questions_missing_answers"] += 1

            qid = q["id"]
            if qid in seen_q_ids:
                stats["duplicate_question_ids"] += 1
            seen_q_ids.add(qid)

            flat_questions.append(q)

    # Save to dedicated output files
    groups_out_path = os.path.join(output_dir, f"svoyak_{prefix}_groups.json")
    questions_out_path = os.path.join(output_dir, f"svoyak_{prefix}_questions.json")
    report_out_path = os.path.join(output_dir, f"svoyak_{prefix}_report.json")

    with open(groups_out_path, "w", encoding="utf-8") as f:
        json.dump(theme_groups, f, ensure_ascii=False, indent=2)

    with open(questions_out_path, "w", encoding="utf-8") as f:
        json.dump(flat_questions, f, ensure_ascii=False, indent=2)

    with open(report_out_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    return stats
