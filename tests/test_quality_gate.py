import pytest
from quality_gate import evaluate_record


def test_clean_record_ready_to_import():
    clean_record = {
        "id": "clean_001",
        "text": "O'zbekiston poytaxti qaysi shahar?",
        "primary_answer": "Toshkent",
        "accepted_answers": [{"text": "Toshkent", "type": "primary"}],
        "points": 10,
        "source": {
            "channel_id": "1758676241",
            "question_message_id": 101,
            "source_name": "Zakovat Baza"
        },
        "editorial": {"status": "ready", "flags": []}
    }
    status, reasons = evaluate_record(clean_record, 0)
    assert status == "ready_to_import"
    assert reasons == []


def test_cyrillic_separate_flags():
    # Cyrillic in primary answer only
    cyr_ans = {
        "text": "O'zbekiston poytaxti qaysi shahar?",
        "primary_answer": "Ташкент",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1}
    }
    status, reasons = evaluate_record(cyr_ans, 0)
    assert status == "rejected"
    assert "cyrillic_in_primary_answer" in reasons
    assert "cyrillic_in_question" not in reasons

    # Cyrillic in question text only
    cyr_q = {
        "text": "Кайси давлат пойтахти Тошкент?",
        "primary_answer": "O'zbekiston",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1}
    }
    status, reasons = evaluate_record(cyr_q, 0)
    assert status == "rejected"
    assert "cyrillic_in_question" in reasons
    assert "cyrillic_in_primary_answer" not in reasons


def test_malformed_accepted_answers():
    # Not a list
    bad_type = {
        "text": "Savol mazmuni bu yerda.",
        "primary_answer": "Javob",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1},
        "accepted_answers": "not_a_list"
    }
    status, reasons = evaluate_record(bad_type, 0)
    assert status == "rejected"
    assert "malformed_accepted_answers" in reasons

    # Item missing text
    bad_item = {
        "text": "Savol mazmuni bu yerda.",
        "primary_answer": "Javob",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1},
        "accepted_answers": [{"type": "primary"}]
    }
    status, reasons = evaluate_record(bad_item, 0)
    assert status == "rejected"
    assert "malformed_accepted_answers" in reasons


def test_missing_provenance():
    no_prov = {
        "text": "Savol mazmuni bu yerda.",
        "primary_answer": "Javob",
        "points": 10,
        "source": {}
    }
    status, reasons = evaluate_record(no_prov, 0)
    assert status == "rejected"
    assert "missing_provenance" in reasons


def test_answer_contamination():
    contaminated = {
        "text": "Ushbu savol matnida Javob: Samarqand deb ko'rsatilgan.",
        "primary_answer": "Samarqand",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1}
    }
    status, reasons = evaluate_record(contaminated, 0)
    assert status == "rejected"
    assert "answer_contamination_in_question" in reasons


def test_answer_length_boundary():
    long_ans = {
        "text": "Savol mazmuni bu yerda.",
        "primary_answer": "A" * 121,
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1}
    }
    status, reasons = evaluate_record(long_ans, 0)
    assert status == "rejected"
    assert "answer_too_long_gt_120" in reasons


def test_needs_review_options_and_punctuation():
    # Options / slash in answer
    opt_record = {
        "text": "Savol mazmuni oxirida nuqta bor.",
        "primary_answer": "Toshkent yoki Samarqand",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1}
    }
    status, reasons = evaluate_record(opt_record, 0)
    assert status == "needs_review"
    assert "answer_contains_options_or_brackets" in reasons

    # Missing terminal punctuation
    no_punct = {
        "text": "Savol oxirida tinish belgisi yo'q",
        "primary_answer": "Toshkent",
        "points": 10,
        "source": {"channel_id": "1", "question_message_id": 1}
    }
    status, reasons = evaluate_record(no_punct, 0)
    assert status == "needs_review"
    assert "missing_terminal_punctuation" in reasons
