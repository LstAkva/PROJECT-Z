from fastapi import status
from quality_gate import evaluate_record
from services.gameplay import normalize_uzbek_latin, is_true_false_match, verify_zanjir_chain


def test_quality_gate_cyrillic_rejection():
    # Record with Cyrillic primary answer
    cyrillic_record = {
        "id": "test_cyr",
        "text": "Moskvaning mashhur harbiy klubi nima deb ataladi?",
        "primary_answer": "ЦСКА",
        "points": 10,
    }
    status_res, reasons = evaluate_record(cyrillic_record, 0)
    assert status_res == "rejected"
    assert "cyrillic_in_primary_answer" in reasons


def test_quality_gate_clean_record_ready_to_import():
    clean_record = {
        "id": "test_clean",
        "text": "Alisher Navoiy qaysi asrda yashab ijod etgan?",
        "primary_answer": "XV asr",
        "points": 20,
        "source": {"source_name": "Zakovat Baza", "channel_id": "1", "question_message_id": 1},
    }
    status_res, reasons = evaluate_record(clean_record, 0)
    assert status_res == "ready_to_import"
    assert len(reasons) == 0


def test_quality_gate_needs_review_triggers():
    # Record with options / yoki in primary answer
    options_record = {
        "id": "test_opt",
        "text": "Kataloniya klubining rasmiy shiori nima deb aytiladi?",
        "primary_answer": "Viska Barsa yoki Viska Kataloniya",
        "points": 40,
        "source": {"source_name": "Zakovat Baza", "channel_id": "1", "question_message_id": 2},
    }
    status_res, reasons = evaluate_record(options_record, 0)
    assert status_res == "needs_review"
    assert "answer_contains_options_or_brackets" in reasons


def test_true_false_synonyms_matching():
    # True variants
    assert is_true_false_match("ha", "rost") is True
    assert is_true_false_match("to'g'ri", "rost") is True
    assert is_true_false_match("true", "rost") is True

    # False variants
    assert is_true_false_match("yo'q", "yolg'on") is True
    assert is_true_false_match("noto'g'ri", "yolg'on") is True
    assert is_true_false_match("false", "yolg'on") is True

    # Cross mismatch
    assert is_true_false_match("ha", "yolg'on") is False
    assert is_true_false_match("yo'q", "rost") is False


def test_e2e_full_tournament_play(client, seed_sample_quiz):
    """
    Simulates a full tournament attempt across all 4 round types:
    Standard -> True/False -> Mantiqasqon -> Zanjir
    """
    quiz = seed_sample_quiz

    # 1. Start Attempt
    start_resp = client.post(f"/api/play/start/{quiz.id}")
    assert start_resp.status_code == status.HTTP_201_CREATED
    data = start_resp.json()
    token = data["session_token"]
    assert token is not None

    # Verify initial state is Round 1 (Standard)
    assert data["current_state"]["round"]["round_type"] == "standard"
    assert data["current_state"]["round_question_index"] == 0

    # 2. Round 1 (Standard): Submit answer
    r1_ans = client.post(f"/api/play/{token}/answer", json={"answer": "  TOSHKENT  "})
    assert r1_ans.status_code == status.HTTP_200_OK
    assert r1_ans.json()["is_correct"] is True
    assert r1_ans.json()["points_awarded"] == 1
    assert r1_ans.json()["round_completed"] is True

    # Guard check: Submitting answer while round_reveal must return 400
    repeat_ans = client.post(f"/api/play/{token}/answer", json={"answer": "Toshkent"})
    assert repeat_ans.status_code == status.HTTP_400_BAD_REQUEST

    # Call /reveal for Round 1
    r1_rev = client.get(f"/api/play/{token}/reveal")
    assert r1_rev.status_code == status.HTTP_200_OK
    assert r1_rev.json()["round_score"] == 1

    # Continue to Round 2 (True/False)
    r1_cont = client.post(f"/api/play/{token}/continue")
    assert r1_cont.status_code == status.HTTP_200_OK
    assert r1_cont.json()["current_state"]["round"]["round_type"] == "true_false"

    # 3. Round 2 (True/False): Submit answer 'ha' which should match 'rost'
    r2_ans = client.post(f"/api/play/{token}/answer", json={"answer": "ha"})
    assert r2_ans.status_code == status.HTTP_200_OK
    assert r2_ans.json()["is_correct"] is True
    assert r2_ans.json()["round_completed"] is True

    client.get(f"/api/play/{token}/reveal")
    r2_cont = client.post(f"/api/play/{token}/continue")
    assert r2_cont.json()["current_state"]["round"]["round_type"] == "mantiqasqon"

    # 4. Round 3 (Mantiqasqon): 2 questions with hidden rule
    # Q1
    client.post(f"/api/play/{token}/answer", json={"answer": "olma"})
    # Q2
    r3_q2 = client.post(f"/api/play/{token}/answer", json={"answer": "limon"})
    assert r3_q2.json()["round_completed"] is True

    # Check that Mantiqasqon reveal contains the hidden rule
    r3_rev = client.get(f"/api/play/{token}/reveal")
    assert r3_rev.json()["config"]["hidden_rule"] == "Barcha javoblar mevalar nomlari"

    r3_cont = client.post(f"/api/play/{token}/continue")
    r4_state = r3_cont.json()["current_state"]
    assert r4_state["round"]["round_type"] == "zanjir"

    # Q1: quyosh (no hint / no chain restriction for Q1)
    q1_res = client.post(f"/api/play/{token}/answer", json={"answer": "quyosh"})
    assert q1_res.json()["is_correct"] is True
    next_q2 = q1_res.json()["next_state"]
    # Zanjir hardening: Ensure NO zanjir_start_letter or previous answer hint is leaked!
    assert "zanjir_start_letter" not in next_q2["question"]
    assert "previous_answer" not in next_q2["question"]
    assert "accepted_answers" not in next_q2["question"]

    # Q2: shahar
    q2_res = client.post(f"/api/play/{token}/answer", json={"answer": "shahar"})
    assert q2_res.json()["is_correct"] is True
    next_q3 = q2_res.json()["next_state"]
    # Ensure NO zanjir_start_letter in Q3 either
    assert "zanjir_start_letter" not in next_q3["question"]
    assert "previous_answer" not in next_q3["question"]

    # Q3: rishton
    q3_res = client.post(f"/api/play/{token}/answer", json={"answer": "rishton"})
    assert q3_res.json()["is_correct"] is True
    assert q3_res.json()["quiz_completed"] is True

    # Reveal Round 4
    r4_rev = client.get(f"/api/play/{token}/reveal")
    assert r4_rev.json()["has_next_round"] is False

    # Finalize attempt
    final_cont = client.post(f"/api/play/{token}/continue")
    assert final_cont.json()["status"] == "completed"

    # 6. Fetch Final Results
    results = client.get(f"/api/play/{token}/results").json()
    assert results["status"] == "completed"
    assert results["total_correct"] == 7
    assert results["total_incorrect"] == 0
    assert results["total_score"] == 9
    assert len(results["rounds"]) == 4
