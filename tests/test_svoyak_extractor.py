import pytest
from services.svoyak_extractor import (
    clean_text,
    parse_theme,
    parse_answer_and_explanation,
    extract_svoyak_groups_from_message,
    generate_stable_id,
)


def test_theme_extraction_with_prefixes():
    # 1-mavzu: Title
    pref, title, raw, is_mat, clue = parse_theme("1-mavzu: Kim qayerda yashaydi")
    assert pref == "1-mavzu"
    assert title == "Kim qayerda yashaydi"
    assert raw == "1-mavzu: Kim qayerda yashaydi"
    assert not is_mat

    # Mavzu: Title
    pref2, title2, raw2, _, _ = parse_theme("Mavzu: Kino olami!")
    assert pref2 == "mavzu"
    assert title2 == "Kino olami!"

    # Unprefixed title
    pref3, title3, _, _, _ = parse_theme("Yunon mifologiyasida ayollar.")
    assert pref3 is None
    assert title3 == "Yunon mifologiyasida ayollar"


def test_five_point_tiers_and_positions():
    msg = {
        "id": 100,
        "date": "2026-01-01T12:00:00",
        "text": (
            "Mavzu: Test Mavzusi\n\n"
            "10. Savol 1?\nJ: Javob 1\n\n"
            "20. Savol 2?\nJ: Javob 2\n\n"
            "30. Savol 3?\nJ: Javob 3\n\n"
            "40. Savol 4?\nJ: Javob 4\n\n"
            "50. Savol 5?\nJ: Javob 5"
        ),
    }
    group = extract_svoyak_groups_from_message(msg, channel_id="12345", channel_name="Test")
    assert group is not None
    assert group["group_status"] == "complete"
    assert group["questions_count"] == 5

    expected_pts = [10, 20, 30, 40, 50]
    for idx, q in enumerate(group["questions"]):
        assert q["points"] == expected_pts[idx]
        assert q["position"] == idx + 1
        assert q["primary_answer"] == f"Javob {idx + 1}"
        assert q["has_answer"] is True
        assert q["editorial"]["status"] == "ready"


def test_multiline_text_punctuation_and_explanation():
    block = (
        "Bu voqea hamon 32,6 million tomoshabin bilan Britaniya televideniyasi tarixidagi eng yuqori baholangan voqeadir?\n"
        "Javob: 1966. Jahon chempionati finali\n"
        "Qabul: 1966 yilgi final\n"
        "Izoh: Ushbu finalda Angliya terma jamoasi GFRni 4:2 hisobida mag'lub etgan."
    )
    q_text, prim, accepted, expl, warnings = parse_answer_and_explanation(block)
    assert "32,6 million tomoshabin" in q_text
    assert prim == "1966. Jahon chempionati finali"
    assert len(accepted) == 2
    assert accepted[0]["text"] == "1966. Jahon chempionati finali"
    assert accepted[1]["text"] == "1966 yilgi final"
    assert expl == "Ushbu finalda Angliya terma jamoasi GFRni 4:2 hisobida mag'lub etgan."
    assert len(warnings) == 0


def test_uzbek_apostrophes_and_cyrillic_support():
    # Cyrillic Жавоб and Uzbek apostrophes (o‘, g‘)
    block = (
        "O‘zbekiston xalq shoiri G‘afur G‘ulomning qaysi asari?\n"
        "Жавоб: Shum bola\n"
        "Қабул: Шум бола\n"
        "Изоҳ: 1936-yilda yozilgan qissa."
    )
    q_text, prim, accepted, expl, warnings = parse_answer_and_explanation(block)
    assert "G‘afur G‘ulomning" in q_text
    assert prim == "Shum bola"
    assert len(accepted) == 2
    assert accepted[1]["text"] == "Шум бола"
    assert expl == "1936-yilda yozilgan qissa."


def test_matrix_theme_preservation():
    msg = {
        "id": 200,
        "date": "2026-01-01T12:00:00",
        "text": (
            "4-mavzu: Matritsali -SKA-\n\n"
            "10. Bu klub chang'i sportidan boshlangan?\nJ: TsSKA\n\n"
            "20. Mariananing eng tubiga tushgan apparat?\nJ: Batiskaf\n\n"
            "30. Qahva brendi?\nJ: Neskafe\n\n"
            "40. Ispaniya provinsiyasi?\nJ: Ueska\n\n"
            "50. Rus qo'shig'i?\nJ: Skazka"
        ),
    }
    group = extract_svoyak_groups_from_message(msg, channel_id="1758676241", channel_name="Svoya igra baza")
    assert group is not None
    assert group["is_matrix_theme"] is True
    assert "-SKA-" in group["matrix_clue"]
    assert group["theme_title"] == "Matritsali -SKA-"

    for q in group["questions"]:
        assert q["is_matrix_theme"] is True
        assert q["matrix_clue"] == "-SKA-"
        assert q["category"] == "Matritsali -SKA-"
        assert "matrix_theme" in q["editorial"]["flags"]


def test_stable_group_and_question_ids():
    ch_id = "1758676241"
    msg_id = 456
    id_1 = generate_stable_id(ch_id, msg_id, 10, 1)
    id_2 = generate_stable_id(ch_id, msg_id, 10, 1)
    assert id_1 == id_2
    assert len(id_1) == 24

    id_diff_pt = generate_stable_id(ch_id, msg_id, 20, 2)
    assert id_1 != id_diff_pt


def test_missing_or_malformed_answers():
    # Question without any answer delimiter
    block = "Bu savolning hech qanday javob belgisi yo'q matni."
    q_text, prim, accepted, expl, warnings = parse_answer_and_explanation(block)
    assert prim is None
    assert accepted == []
    assert "missing_answer_delimiter" in warnings

    msg = {
        "id": 300,
        "date": "2026-01-01T12:00:00",
        "text": (
            "Mavzu: Javobsiz Savollar\n\n"
            "10. Savol 1 matni javobsiz qolgan?\n\n"
            "20. Savol 2 matni\nJ: Bor javob\n\n"
            "30. Savol 3\nJ: Bor javob 3\n\n"
            "40. Savol 4\nJ: Bor javob 4\n\n"
            "50. Savol 5\nJ: Bor javob 5"
        ),
    }
    group = extract_svoyak_groups_from_message(msg, channel_id="999", channel_name="Test")
    assert group is not None
    q1 = group["questions"][0]
    assert q1["has_answer"] is False
    assert q1["primary_answer"] is None
    assert q1["editorial"]["status"] == "needs_review"
    assert "missing_answer" in q1["editorial"]["flags"]

    q2 = group["questions"][1]
    assert q2["has_answer"] is True
    assert q2["primary_answer"] == "Bor javob"
    assert q2["editorial"]["status"] == "ready"


def test_incomplete_or_irregular_groups():
    # Group with only 3 questions (10, 20, 30)
    msg_incomplete = {
        "id": 401,
        "text": (
            "Mavzu: Chala Mavzu\n\n"
            "10. Savol 1\nJ: Ans 1\n\n"
            "20. Savol 2\nJ: Ans 2\n\n"
            "30. Savol 3\nJ: Ans 3"
        ),
    }
    group_inc = extract_svoyak_groups_from_message(msg_incomplete, channel_id="999")
    assert group_inc is not None
    assert group_inc["group_status"] == "incomplete"
    assert group_inc["questions_count"] == 3
    assert any("fewer_than_5_questions" in w for w in group_inc["warnings"])

    # Group with duplicate points (irregular)
    msg_irregular = {
        "id": 402,
        "text": (
            "Mavzu: Qaytalanuvchi\n\n"
            "10. S1\nJ: A1\n\n"
            "10. S2\nJ: A2\n\n"
            "20. S3\nJ: A3\n\n"
            "30. S4\nJ: A4\n\n"
            "40. S5\nJ: A5\n\n"
            "50. S6\nJ: A6"
        ),
    }
    group_irr = extract_svoyak_groups_from_message(msg_irregular, channel_id="999")
    assert group_irr is not None
    assert group_irr["group_status"] == "irregular"
    assert group_irr["questions_count"] == 6


def test_original_source_text_preserved():
    original_text = (
        "1-mavzu: Tabiat sirlari\n\n"
        "10. Savol matni o'zbek tilida?\nJ: Javob\n\n"
        "20. Ikkinchi savol?\nJ: Ikkinchi javob\n\n"
        "30. Uchinchi savol?\nJ: Uchinchi javob\n\n"
        "40. To'rtinchi savol?\nJ: To'rtinchi javob\n\n"
        "50. Beshinchi savol?\nJ: Beshinchi javob"
    )
    msg = {"id": 501, "date": "2026-05-01T10:00:00", "text": original_text}
    group = extract_svoyak_groups_from_message(msg, channel_id="ch_1")
    assert group["raw_message_text"] == original_text
    assert group["theme_raw"] == "1-mavzu: Tabiat sirlari"
    # Individual question raw blocks contain their markers
    assert "10. Savol matni o'zbek tilida?" in group["questions"][0]["raw_block"]
