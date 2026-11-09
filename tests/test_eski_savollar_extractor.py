"""
Unit tests for the Eski Savollar profiling and Daily Question extraction pipeline.
Verifies text cleaning, header parsing, answer splitting, delimiter variants,
explanation/alternative extraction, media dependency auditing, multipart detection,
reply/sequence resolution, duplicate detection, and classification buckets.
"""

import os
import json
import pytest
from services.eski_savollar_extractor import (
    clean_telegram_text,
    normalize_text_for_comparison,
    parse_question_header,
    parse_answer_components,
    split_question_and_answer,
    detect_media_dependency,
    detect_multipart,
    extract_eski_savollar,
)


# ----------------------------------------------------------------------------
# 1. Text Cleaning & Normalization Tests
# ----------------------------------------------------------------------------

def test_clean_telegram_text_string():
    raw = "Oʻzbekiston intellektual klubi"
    assert clean_telegram_text(raw) == "Oʻzbekiston intellektual klubi"


def test_clean_telegram_text_entities():
    entities = [
        "Bu ",
        {"type": "bold", "text": "Alisher Navoiy"},
        " qalamiga mansub asar."
    ]
    assert clean_telegram_text(entities) == "Bu Alisher Navoiy qalamiga mansub asar."


def test_clean_telegram_text_zero_width_and_falsy():
    assert clean_telegram_text(None) == ""
    assert clean_telegram_text("") == ""
    assert clean_telegram_text("Savol\u200b matni\ufeff") == "Savol matni"


def test_normalize_text_for_comparison():
    t1 = "12. Dunyoda turli fanlar koʻp! Ularning barchasi nimanidir oʻrganadi?"
    t2 = "#Savol_12: Dunyoda turli fanlar ko'p... Ularning barchasi nimanidir o'rganadi."
    n1 = normalize_text_for_comparison(t1)
    n2 = normalize_text_for_comparison(t2)
    assert "dunyoda turli fanlar" in n1
    assert "dunyoda turli fanlar" in n2


# ----------------------------------------------------------------------------
# 2. Question Header & Banner Parsing Tests
# ----------------------------------------------------------------------------

def test_parse_question_header_numbered():
    raw = "14893.\n\nVestminster abbatligi - bu xristianlarning ulkan qabristoni..."
    q_num, genre, raw_head, body = parse_question_header(raw)
    assert q_num == 14893
    assert body.startswith("Vestminster abbatligi")
    assert raw_head.startswith("14893.")


def test_parse_question_header_with_emojis_and_banners():
    raw = "🧠 Savol!\n\n9646.\n\n⚠️ Kunning oxirgi savoli\n\nAntik davrlarda hindlar deb kimlarni atashgan?"
    q_num, genre, raw_head, body = parse_question_header(raw)
    assert q_num == 9646
    assert "Kunning oxirgi savoli" in (genre or "")
    assert body.startswith("Antik davrlarda hindlar")


def test_parse_question_header_hashtag_and_cyrillic():
    raw = "#Savol_42:\n\nСавол матни шу ерда."
    q_num, genre, raw_head, body = parse_question_header(raw)
    assert q_num == 42
    assert body == "Савол матни шу ерда."


# ----------------------------------------------------------------------------
# 3. Answer Splitting & Delimiter Variations
# ----------------------------------------------------------------------------

def test_split_inline_qa_standard():
    text = "Bu davlat qaysi?\n\nJavob: Papua Yangi Gvineyasi"
    q_part, pa, acc, exp, src, auth, warns = split_question_and_answer(text)
    assert q_part == "Bu davlat qaysi?"
    assert pa == "Papua Yangi Gvineyasi"
    assert acc == []
    assert exp is None
    assert warns == []


@pytest.mark.parametrize("delimiter,answer_text", [
    ("Javob:", "Papua Yangi Gvineyasi"),
    ("Javob.", "Papua Yangi Gvineyasi"),
    ("Javob -", "Papua Yangi Gvineyasi"),
    ("Жавоб:", "Папуа Янги Гвинеяси"),
    ("Javobi:", "Papua Yangi Gvineyasi"),
    ("J:", "Papua Yangi Gvineyasi"),
    ("✅ Javob:", "Papua Yangi Gvineyasi"),
])
def test_split_inline_qa_delimiters(delimiter, answer_text):
    text = f"Savol matni.\n\n{delimiter} {answer_text}"
    q_part, pa, acc, exp, src, auth, warns = split_question_and_answer(text)
    assert q_part == "Savol matni."
    assert pa == answer_text.rstrip(".")


# ----------------------------------------------------------------------------
# 4. Explanation, Accepted Alternatives, Source & Author
# ----------------------------------------------------------------------------

def test_answer_components_izoh_qabul_source_author():
    ans_block = (
        "Filbonlar\n\n"
        "Qabul: Fil haydovchilar, fil yetaklovchilar\n\n"
        "Izoh: Fillarni birinchi boʻlib Hindistonda qoʻlga oʻrgatishgan.\n\n"
        "Manba: Tarixiy ensiklopediya\n\n"
        "Muallif: Alisher Roʻziyev"
    )
    pa, acc, exp, src, auth, warns = parse_answer_components(ans_block)
    assert pa == "Filbonlar"
    assert "Fil haydovchilar" in acc
    assert "fil yetaklovchilar" in acc
    assert exp == "Fillarni birinchi boʻlib Hindistonda qoʻlga oʻrgatishgan."
    assert src == "Tarixiy ensiklopediya"
    assert auth == "Alisher Roʻziyev"


def test_parenthetical_accepted_answers():
    ans_block = "Morskoy boy (dengiz jangi)"
    pa, acc, exp, src, auth, warns = parse_answer_components(ans_block)
    assert pa == "Morskoy boy"
    assert "dengiz jangi" in acc


# ----------------------------------------------------------------------------
# 5. Media Dependency & Multipart Detection
# ----------------------------------------------------------------------------

def test_detect_media_dependency_essential():
    text = "Ushbu rasmda koʻrib turganingizdek chap tomondagi odam kim?"
    msg = {"photo": "photos/file_1.jpg"}
    media_info = detect_media_dependency(text, msg, local_media_dir=None)
    assert media_info["has_media_attachment"] is True
    assert media_info["is_essential_visual"] is True
    assert media_info["local_media_present"] is False


def test_detect_media_narrative_not_blocked():
    text = "Pablo Pikassoning mashhur rasmi 'Gernika' 1937-yilda yaratilgan. Savol..."
    msg = {}
    media_info = detect_media_dependency(text, msg, local_media_dir=None)
    assert media_info["has_media_attachment"] is False
    assert media_info["is_essential_visual"] is False


def test_detect_media_dependency_suffix_variations_and_no_false_positives():
    """
    Focused regression test:
    1. Suffix variations such as 'rasmdagidan', 'suratdagidan' are detected as essential visual dependencies.
    2. Non-visual words with 'rasm' prefix (e.g., 'rasmiy', 'rasman') are NOT flagged as false positives.
    """
    # Suffix variation: rasmdagidan without photo attachment
    text_suffix = "Istanbuldagi masjidlarda deyarli har birida shunaqa chiroyli naqsh qilingan rasmdagidan ko'rish mumkin."
    info_suffix = detect_media_dependency(text_suffix, {}, local_media_dir=None)
    assert info_suffix["is_essential_visual"] is True

    # Suffix variation: suratdagidan without photo attachment
    text_surat = "Ushbu suratdagidan farqli ravishda boshqa bino tasvirlangan."
    info_surat = detect_media_dependency(text_surat, {}, local_media_dir=None)
    assert info_surat["is_essential_visual"] is True

    # False positive test: 'rasmiy' (official) and 'rasman'
    text_rasmiy = "O'zbekiston Respublikasining rasmiy delegatsiyasi Parij shahriga yetib bordi."
    info_rasmiy = detect_media_dependency(text_rasmiy, {}, local_media_dir=None)
    assert info_rasmiy["is_essential_visual"] is False

    text_rasman = "Toshkent shahrida rasman yangi metro bekati ochildi."
    info_rasman = detect_media_dependency(text_rasman, {}, local_media_dir=None)
    assert info_rasman["is_essential_visual"] is False


def test_detect_multipart_questions():
    text = (
        "Duplet!\n"
        "1. Birinchi savol matni?\n"
        "2. Ikkinchi savol matni?"
    )
    is_multi, subqs = detect_multipart(text)
    assert is_multi is True
    assert len(subqs) == 2


# ----------------------------------------------------------------------------
# 6. Pipeline Execution & Classification Verification
# ----------------------------------------------------------------------------

def test_pipeline_on_synthetic_data(tmp_path):
    synthetic_export = {
        "name": "Eski Savollar",
        "type": "public_channel",
        "id": 2010763825,
        "messages": [
            # 1. Service message
            {"id": 1, "type": "service", "date": "2023-10-27T10:00:00", "action": "create_channel"},
            # 2. Eligible candidate (standard inline)
            {
                "id": 2, "type": "message", "date": "2023-10-27T10:05:00",
                "text": "1. Bu davlat Etnograf uchun jannat deb ataladi. Qaysi mamlakat?\n\nJavob: Papua Yangi Gvineyasi"
            },
            # 3. Blocked missing media (essential visual)
            {
                "id": 3, "type": "message", "date": "2023-10-27T10:10:00",
                "photo": "photos/file_3.jpg",
                "text": "2. Mazkur rasmda berkitib qoʻyilgan soʻzlarni toping?\n\nJavob: LiOn"
            },
            # 4. Blocked missing answer (unanswered question)
            {
                "id": 4, "type": "message", "date": "2023-10-27T10:15:00",
                "text": "3. Bu savolning javobi hech qachon yozilmagan edi va kanal davom etdi."
            },
            # 5. Needs review (multipart question)
            {
                "id": 5, "type": "message", "date": "2023-10-27T10:20:00",
                "text": "4. Duplet:\n1. Savol bir?\n2. Savol ikki?\n\nJavob: 1. A 2. B"
            },
            # 6. Excluded noise (admin announcement)
            {
                "id": 6, "type": "message", "date": "2023-10-27T10:25:00",
                "text": "Kanaldagi savollar soni 50 ta boʻldi. Reklama qilishda davom eting!"
            },
            # 7. Duplicate of Question 2 (should be marked needs_review duplicate)
            {
                "id": 7, "type": "message", "date": "2023-10-27T10:30:00",
                "text": "5. Bu davlat Etnograf uchun jannat deb ataladi. Qaysi mamlakat?\n\nJavob: Papua Yangi Gvineyasi"
            },
            # 8. Split Q&A: Question in 8, answered via Reply in 9
            {
                "id": 8, "type": "message", "date": "2023-10-27T10:35:00",
                "text": "6. Qohirada yashovchi erkak xotini bilan nima qilgan?"
            },
            {
                "id": 9, "type": "message", "date": "2023-10-27T10:36:00",
                "reply_to_message_id": 8,
                "text": "Javob: Yashirincha ajrashgan"
            }
        ]
    }

    input_file = tmp_path / "test_result.json"
    output_dir = tmp_path / "extracted_output"

    with open(input_file, "w", encoding="utf-8") as f:
        json.dump(synthetic_export, f)

    res = extract_eski_savollar(str(input_file), str(output_dir))

    # Assert output structure
    assert res["total_messages"] == 9
    assert res["questions_saved"] == 9
    cls = res["classifications"]

    assert cls["excluded_non_question"] >= 2  # service msg and announcement
    assert cls["eligible_candidate"] >= 2     # msg 2 and resolved split msg 8
    assert cls["blocked_missing_media"] == 1  # msg 3
    assert cls["blocked_missing_answer"] == 1 # msg 4
    assert cls["needs_review"] >= 2          # msg 5 (multipart) and msg 7 (duplicate)

    # Check generated files exist and parse as valid JSON
    assert os.path.exists(output_dir / "eski_savollar_questions.json")
    assert os.path.exists(output_dir / "eski_savollar_profiling_report.json")
    assert os.path.exists(output_dir / "eski_savollar_media_inventory.json")
    assert os.path.exists(output_dir / "eski_savollar_duplicates_report.json")

    with open(output_dir / "eski_savollar_questions.json", "r", encoding="utf-8") as qf:
        qs = json.load(qf)
        assert len(qs) == 9
        # Check deterministic ID format
        assert qs[1]["question_id"] == "eski_2010763825_2"
        # Check resolved split question (msg 8)
        q8 = next(q for q in qs if q["message_id"] == 8)
        assert q8["resolution_method"] == "reply_to"
        assert q8["primary_answer"] == "Yashirincha ajrashgan"
        assert q8["classification"] == "eligible_candidate"
