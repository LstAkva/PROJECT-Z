from fastapi import status


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "ok"


def test_quiz_discovery_and_detail(client, seed_sample_quiz):
    quiz = seed_sample_quiz

    # 1. List quizzes
    response = client.get("/api/quizzes")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data) >= 1
    assert data[0]["quiz_id"] == quiz.id
    assert data[0]["title"] == "ZakoWhat Test Championship"
    assert data[0]["description"] == "Automated verification quiz."
    assert data[0]["version_number"] == 1

    # 2. Get quiz detail
    detail_res = client.get(f"/api/quizzes/{quiz.id}")
    assert detail_res.status_code == status.HTTP_200_OK
    detail = detail_res.json()
    assert detail["title"] == "ZakoWhat Test Championship"
    assert len(detail["rounds"]) == 4

    # Verify no answers or hidden rules are leaked
    for r in detail["rounds"]:
        if r["round_type"] == "mantiqasqon":
            assert "hidden_rule" not in r["config"]
        for q in r["questions"]:
            assert "accepted_answers" not in q
            assert "correct_answers" not in q


def test_full_gameplay_lifecycle_and_zanjir(client, seed_sample_quiz):
    quiz = seed_sample_quiz

    # =========================================================================
    # 1. START SOLO ATTEMPT
    # =========================================================================
    start_res = client.post(f"/api/play/start/{quiz.id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    start_data = start_res.json()
    session_token = start_data["session_token"]
    assert session_token is not None

    # Current state should be Round 1, Question 1 (Standard)
    curr = start_data["current_state"]
    assert curr["round"]["round_type"] == "standard"
    assert curr["round_question_index"] == 0
    assert "Toshkent" not in str(curr)

    # =========================================================================
    # 2. ROUND 1 (Standard): Submit Answer
    # =========================================================================
    ans1_res = client.post(f"/api/play/{session_token}/answer", json={"answer": "toshkent"})
    assert ans1_res.status_code == status.HTTP_200_OK
    ans1_data = ans1_res.json()
    assert ans1_data["is_correct"] is True
    assert ans1_data["points_awarded"] == 1
    assert ans1_data["total_score"] == 1
    assert ans1_data["round_completed"] is True
    assert ans1_data["reveal_available"] is True

    # Trying to answer again during round_reveal must be rejected
    re_ans = client.post(f"/api/play/{session_token}/answer", json={"answer": "anything"})
    assert re_ans.status_code == status.HTTP_400_BAD_REQUEST

    # Call /reveal for Round 1
    rev1_res = client.get(f"/api/play/{session_token}/reveal")
    assert rev1_res.status_code == status.HTTP_200_OK
    rev1_data = rev1_res.json()
    assert rev1_data["round_type"] == "standard"
    assert rev1_data["round_score"] == 1
    assert rev1_data["questions"][0]["submitted_answer"] == "toshkent"
    assert "Toshkent" in rev1_data["questions"][0]["correct_answers"]

    # Continue to next round
    cont1_res = client.post(f"/api/play/{session_token}/continue")
    assert cont1_res.status_code == status.HTTP_200_OK
    cont1_data = cont1_res.json()
    assert cont1_data["status"] == "in_progress"
    assert cont1_data["current_state"]["round"]["round_type"] == "true_false"

    # =========================================================================
    # 3. ROUND 2 (True/False): Submit Answer
    # =========================================================================
    ans2_res = client.post(f"/api/play/{session_token}/answer", json={"answer": "rost"})
    assert ans2_res.status_code == status.HTTP_200_OK
    ans2_data = ans2_res.json()
    assert ans2_data["is_correct"] is True
    assert ans2_data["round_completed"] is True

    # Continue to Round 3 (Mantiqasqon)
    client.get(f"/api/play/{session_token}/reveal")
    cont2_res = client.post(f"/api/play/{session_token}/continue")
    assert cont2_res.json()["current_state"]["round"]["round_type"] == "mantiqasqon"

    # =========================================================================
    # 4. ROUND 3 (Mantiqasqon): 2 Questions with Hidden Rule
    # =========================================================================
    # Q1
    q3_1 = client.post(f"/api/play/{session_token}/answer", json={"answer": "olma"})
    assert q3_1.json()["is_correct"] is True
    assert q3_1.json()["round_completed"] is False

    # Q2
    q3_2 = client.post(f"/api/play/{session_token}/answer", json={"answer": "limon"})
    assert q3_2.json()["is_correct"] is True
    assert q3_2.json()["round_completed"] is True

    # Check Mantiqasqon reveal contains the hidden_rule
    rev3_res = client.get(f"/api/play/{session_token}/reveal")
    assert rev3_res.status_code == status.HTTP_200_OK
    assert rev3_res.json()["config"]["hidden_rule"] == "Barcha javoblar mevalar nomlari"

    # Continue to Round 4 (Zanjir)
    cont3_res = client.post(f"/api/play/{session_token}/continue")
    assert cont3_res.json()["current_state"]["round"]["round_type"] == "zanjir"

    # =========================================================================
    # 5. ROUND 4 (Zanjir): Chain rule verification
    # =========================================================================
    # Q1: quyosh (no chain rule for Q1)
    z1 = client.post(f"/api/play/{session_token}/answer", json={"answer": "quyosh"})
    assert z1.json()["is_correct"] is True

    # Q2: shahar (quyosh ends in 'sh', shahar starts with 'sh')
    z2 = client.post(f"/api/play/{session_token}/answer", json={"answer": "shahar"})
    assert z2.json()["is_correct"] is True

    # Q3: rishton (shahar ends in 'r', rishton starts with 'r')
    z3 = client.post(f"/api/play/{session_token}/answer", json={"answer": "rishton"})
    assert z3.json()["is_correct"] is True
    assert z3.json()["round_completed"] is True
    assert z3.json()["quiz_completed"] is True

    # Reveal Round 4
    rev4 = client.get(f"/api/play/{session_token}/reveal")
    assert rev4.status_code == status.HTTP_200_OK
    assert rev4.json()["has_next_round"] is False

    # Final Continue marks attempt as completed
    final_cont = client.post(f"/api/play/{session_token}/continue")
    assert final_cont.status_code == status.HTTP_200_OK
    assert final_cont.json()["status"] == "completed"

    # =========================================================================
    # 6. RESULTS
    # =========================================================================
    results_res = client.get(f"/api/play/{session_token}/results")
    assert results_res.status_code == status.HTTP_200_OK
    results = results_res.json()
    assert results["status"] == "completed"
    assert results["total_correct"] == 7
    assert results["total_incorrect"] == 0
    # Score: R1(1) + R2(1) + R3(2+2) + R4(1+1+1) = 1 + 1 + 4 + 3 = 9
    assert results["total_score"] == 9
    assert len(results["rounds"]) == 4


def test_zanjir_chain_violation_marked_incorrect(client, seed_sample_quiz):
    """
    Verifies that if a user submits a valid synonym or correct content that
    violates the chain rule, it is marked incorrect.
    """
    quiz = seed_sample_quiz
    start = client.post(f"/api/play/start/{quiz.id}").json()
    token = start["session_token"]

    # Fast-forward R1, R2, R3
    client.post(f"/api/play/{token}/answer", json={"answer": "Toshkent"})
    client.post(f"/api/play/{token}/continue")
    client.post(f"/api/play/{token}/answer", json={"answer": "rost"})
    client.post(f"/api/play/{token}/continue")
    client.post(f"/api/play/{token}/answer", json={"answer": "olma"})
    client.post(f"/api/play/{token}/answer", json={"answer": "limon"})
    client.post(f"/api/play/{token}/continue")

    # In Round 4 (Zanjir):
    # Q1: quyosh (ends in 'sh')
    ans_q1 = client.post(f"/api/play/{token}/answer", json={"answer": "quyosh"}).json()
    assert ans_q1["is_correct"] is True
    # Verify no hint in next_state question
    assert "zanjir_start_letter" not in ans_q1["next_state"]["question"]
    assert "previous_answer" not in ans_q1["next_state"]["question"]
    assert "accepted_answers" not in ans_q1["next_state"]["question"]

    # Also verify GET /api/play/{token} does not leak hint
    current_q2_state = client.get(f"/api/play/{token}").json()
    assert "zanjir_start_letter" not in current_q2_state["question"]

    # Q2: suppose a user submits an answer that violates the chain rule ('buxoro' doesn't start with 'sh' or 's')
    ans_q2_wrong = client.post(f"/api/play/{token}/answer", json={"answer": "buxoro"}).json()
    assert ans_q2_wrong["is_correct"] is False
    assert ans_q2_wrong["points_awarded"] == 0
    # Must NOT leak expected letter or chain link in the submission response
    assert "zanjir_start_letter" not in ans_q2_wrong
    assert "expected_letter" not in ans_q2_wrong
    assert "hint" not in ans_q2_wrong
