import pytest
from datetime import datetime, timezone
from fastapi import status
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt
from api.quizzes import compile_published_manifest


def test_incomplete_draft_can_save_but_cannot_publish(client, db_session):
    """
    Verify that incomplete drafts (missing questions or answers) can be saved freely,
    but attempting to publish fails with 422 Unprocessable Entity and structured errors.
    """
    # 1. Create draft
    res = client.post("/api/drafts", json={"title": "Incomplete Draft", "game_mode": "modern_multiround"})
    assert res.status_code == status.HTTP_201_CREATED
    draft_id = res.json()["version_id"]

    # 2. Attempt publish with 0 rounds -> 422
    pub_res = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    data = pub_res.json()["detail"]
    assert "errors" in data
    assert any("kamida bitta raund" in e for e in data["errors"])

    # 3. Add 1 round with 0 questions -> can save, but publish fails with 422
    r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zanjir"})
    assert r_res.status_code == status.HTTP_201_CREATED

    pub_res2 = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_res2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    data2 = pub_res2.json()["detail"]
    assert any("hech qanday savol yo'q" in e for e in data2["errors"])

    # Verify draft remains status='draft' in DB
    v = db_session.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    assert v.status == "draft"
    assert v.published_manifest is None


def test_classic_zakovat_validation_enforces_exact_2_rounds_12_questions(client, db_session):
    """
    Requirement: For classic_zakovat, publish validation must treat anything other than
    exactly 2 sequential rounds x exactly 12 questions with sequences 1..12 as blocking errors.
    """
    # 1. Create classic zakovat draft
    res = client.post("/api/drafts", json={"title": "Zakovat Bahor 2026", "game_mode": "classic_zakovat"})
    draft_id = res.json()["version_id"]

    # Add only 1 round of 12 questions -> should fail INVALID_ROUND_COUNT
    r1 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "rasmiyatchilik"}).json()
    round1_id = r1["round_id"]
    for i in range(1, 13):
        client.post(f"/api/drafts/{draft_id}/rounds/{round1_id}/questions", json={
            "text": f"R1 Savol {i}",
            "primary_answer": f"Javob {i}",
            "points": 1,
        })

    # Validate
    val1 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val1["valid"] is False
    codes1 = [e["code"] for e in val1["structured_errors"]]
    assert "INVALID_ROUND_COUNT" in codes1

    # Publish attempt must fail with 422
    pub1 = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub1.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # Add second round with only 11 questions -> should fail INVALID_QUESTION_COUNT
    r2 = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "rasmiyatchilik"}).json()
    round2_id = r2["round_id"]
    for i in range(1, 12):
        client.post(f"/api/drafts/{draft_id}/rounds/{round2_id}/questions", json={
            "text": f"R2 Savol {i}",
            "primary_answer": f"Javob {i}",
            "points": 1,
        })

    val2 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val2["valid"] is False
    codes2 = [e["code"] for e in val2["structured_errors"]]
    assert "INVALID_QUESTION_COUNT" in codes2

    # Add 12th question to round 2 -> now exactly 2 rounds x 12 questions = 24 questions
    client.post(f"/api/drafts/{draft_id}/rounds/{round2_id}/questions", json={
        "text": "R2 Savol 12",
        "primary_answer": "Javob 12",
        "points": 1,
    })

    val3 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val3["valid"] is True
    assert len(val3["structured_errors"]) == 0
    assert val3["total_rounds"] == 2
    assert val3["total_questions"] == 24

    # Publish now succeeds
    pub_ok = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_ok.status_code == status.HTTP_200_OK
    assert pub_ok.json()["status"] == "published"


def test_classic_zakovat_validation_rejects_single_round_24_questions(client, db_session):
    """
    Requirement: Classic Zakovat requires the authentic 2 tur x 12 questions structure.
    A single round of 24 questions is rejected with INVALID_ROUND_COUNT.
    """
    res = client.post("/api/drafts", json={"title": "Single Round 24 Q Pack", "game_mode": "classic_zakovat"})
    draft_id = res.json()["version_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zakovat_classic"}).json()
    round_id = r["round_id"]

    for i in range(1, 25):
        client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
            "text": f"Savol {i}",
            "primary_answer": f"Javob {i}",
            "points": 1,
        })

    val = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val["valid"] is False
    assert any(e["code"] == "INVALID_ROUND_COUNT" for e in val["structured_errors"])

    # Attempting to publish must fail
    pub_fail = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub_fail.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_classic_zakovat_validation_rejects_invalid_round_counts(client, db_session):
    """
    Classic Zakovat must reject 3+ rounds, or 1 round of an unsupported round_type.
    """
    # Case A: 3 rounds
    res = client.post("/api/drafts", json={"title": "3-Round Classic", "game_mode": "classic_zakovat"})
    draft_id = res.json()["version_id"]
    for i in range(1, 4):
        client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zakovat_classic"})

    val3 = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val3["valid"] is False
    assert any(e["code"] == "INVALID_ROUND_COUNT" for e in val3["structured_errors"])

    # Case B: 1 round of invalid type (not zakovat_classic, e.g. rasmiyatchilik)
    res_b = client.post("/api/drafts", json={"title": "1-Round Rasmiyatchilik Classic", "game_mode": "classic_zakovat"})
    draft_id_b = res_b.json()["version_id"]
    r_b = client.post(f"/api/drafts/{draft_id_b}/rounds", json={"round_type": "rasmiyatchilik"}).json()
    for i in range(1, 25):
        client.post(f"/api/drafts/{draft_id_b}/rounds/{r_b['round_id']}/questions", json={
            "text": f"Rasmiyatchilik savol {i}",
            "primary_answer": f"Javob {i}",
            "points": 1,
        })

    val_b = client.post(f"/api/drafts/{draft_id_b}/validate").json()
    assert val_b["valid"] is False
    assert any(e["code"] == "INVALID_ROUND_COUNT" for e in val_b["structured_errors"])


def test_mantiqqasqon_requires_hidden_rule_and_keeps_it_hidden(client, db_session):
    """
    Requirement: For mantiqqasqon, publish validation must require the hidden rule/logic
    in round config. In preview and gameplay, hidden_rule must remain hidden until reveal.
    """
    res = client.post("/api/drafts", json={"title": "Mantiqqasqon Quiz", "game_mode": "modern_multiround"})
    draft_id = res.json()["version_id"]

    # 1. Add mantiqqasqon round WITHOUT hidden_rule in config
    r = client.post(f"/api/drafts/{draft_id}/rounds", json={
        "round_type": "mantiqqasqon",
        "config": {"theme": "Tarixiy sanalar"}
    }).json()
    round_id = r["round_id"]

    client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "1-savol mantiq",
        "primary_answer": "Navoiy",
        "points": 1,
    })

    # Validate -> should fail with MISSING_HIDDEN_RULE
    val = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val["valid"] is False
    assert any(e["code"] == "MISSING_HIDDEN_RULE" for e in val["structured_errors"])

    # 2. Update round config to include hidden_rule
    db_round = db_session.query(Round).filter(Round.id == round_id).first()
    db_round.config = {"theme": "Tarixiy sanalar", "hidden_rule": "Barcha javoblar 15-asr allomalari"}
    db_session.commit()

    val_ok = client.post(f"/api/drafts/{draft_id}/validate").json()
    assert val_ok["valid"] is True

    # 3. Check Preview endpoint: hidden_rule must be STRIPPED from preview config
    prev = client.get(f"/api/drafts/{draft_id}/preview").json()
    r_prev = prev["rounds"][0]
    assert "hidden_rule" not in r_prev["config"]
    assert "theme" in r_prev["config"]

    # 4. Start Preview Simulation: hidden_rule must also be hidden in question state
    sim = client.post(f"/api/drafts/{draft_id}/preview/start").json()
    assert sim["is_preview"] is True
    assert "hidden_rule" not in sim["current_state"]["round"]["config"]


def test_preview_endpoint_sanitizes_answers_and_explanations(client, db_session):
    """
    Requirement: Preview endpoint must not expose accepted answers or hidden metadata.
    """
    res = client.post("/api/drafts", json={"title": "Secret Answers Draft"})
    draft_id = res.json()["version_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "gulmisiz_rayhonmisiz"}).json()
    round_id = r["round_id"]

    client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "O'zbekiston poytaxti qaysi shahar?",
        "options": [
            {"key": "A", "text": "Toshkent"},
            {"key": "B", "text": "Samarqand"},
            {"key": "C", "text": "Buxoro"},
            {"key": "D", "text": "Xiva"},
        ],
        "correct_option": "A",
        "explanation": "Toshkent 1930 yildan beri poytaxt",
        "points": 2,
    })

    prev = client.get(f"/api/drafts/{draft_id}/preview")
    assert prev.status_code == status.HTTP_200_OK
    data = prev.json()

    assert data["title"] == "Secret Answers Draft"
    assert len(data["rounds"]) == 1
    q = data["rounds"][0]["questions"][0]

    assert q["text"] == "O'zbekiston poytaxti qaysi shahar?"
    assert q["points"] == 2
    # Check that answers and explanations are completely stripped
    assert "primary_answer" not in q
    assert "accepted_answers" not in q
    assert "all_accepted_answers" not in q
    assert "explanation" not in q
    # Check that options are sanitized (only key and text, no is_correct)
    assert len(q["options"]) == 4
    for opt in q["options"]:
        assert "key" in opt and "text" in opt
        assert "is_correct" not in opt


def test_preview_simulation_is_isolated_from_public_gameplay(client, db_session):
    """
    Requirement: Preview simulation must remain isolated from public gameplay.
    A draft quiz must NEVER be playable via public /api/play/start/{quiz_id}.
    """
    res = client.post("/api/drafts", json={"title": "Draft Play Isolation Quiz"})
    draft_id = res.json()["version_id"]
    quiz_id = res.json()["quiz_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "zanjir"}).json()
    round_id = r["round_id"]
    client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "Zanjir savoli",
        "primary_answer": "Zanjir",
        "points": 1,
    })

    # 1. Public play endpoint MUST reject draft quiz with 404
    pub_play = client.post(f"/api/play/start/{quiz_id}")
    assert pub_play.status_code == status.HTTP_404_NOT_FOUND

    # 2. Preview simulation starts successfully
    sim_res = client.post(f"/api/drafts/{draft_id}/preview/start")
    assert sim_res.status_code == status.HTTP_201_CREATED
    sim_data = sim_res.json()
    token = sim_data["session_token"]
    assert sim_data["is_preview"] is True

    # 3. Creator can play inside preview session
    sub_res = client.post(f"/api/play/{token}/answer", json={
        "answer": "Zanjir",
    })
    assert sub_res.status_code == status.HTTP_200_OK
    assert sub_res.json()["is_correct"] is True

    # 4. Advance past round reveal to complete quiz
    cont_res = client.post(f"/api/play/{token}/continue")
    assert cont_res.status_code == status.HTTP_200_OK
    assert cont_res.json()["quiz_completed"] is True

    # 5. Result reflects is_preview
    results = client.get(f"/api/play/{token}/results")
    assert results.status_code == status.HTTP_200_OK
    assert results.json()["is_preview"] is True


def test_publishing_is_idempotency_safe(client, db_session):
    """
    Requirement: Subsequent publish attempt on an already-published version must
    return HTTP 409 Conflict without modifying the manifest or timestamp.
    """
    # 1. Create and complete a valid draft
    res = client.post("/api/drafts", json={"title": "Idempotent Quiz", "game_mode": "svoyak"})
    draft_id = res.json()["version_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={
        "round_type": "vabank",
        "config": {"theme": "Geografiya"}
    }).json()
    client.post(f"/api/drafts/{draft_id}/rounds/{r['round_id']}/questions", json={
        "text": "Dunyoning eng baland cho'qqisi?",
        "primary_answer": "Everest",
        "points": 10,
    })

    # 2. First publish -> 200 OK
    pub1 = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub1.status_code == status.HTTP_200_OK
    data1 = pub1.json()
    assert data1["status"] == "published"
    pub_time_1 = data1["published_at"]

    # DB verify
    v = db_session.query(QuizVersion).filter(QuizVersion.id == draft_id).first()
    assert v.status == "published"
    assert v.published_at is not None
    orig_manifest = v.published_manifest

    # 3. Second publish -> 409 Conflict
    pub2 = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub2.status_code == status.HTTP_409_CONFLICT
    assert "allaqachon e'lon qilingan" in pub2.json()["detail"]

    # Verify manifest and timestamp were untouched
    db_session.refresh(v)
    assert v.published_at.isoformat() == pub_time_1
    assert v.published_manifest == orig_manifest


def test_published_gameplay_is_self_contained_and_immutable(client, db_session):
    """
    Requirement: Published gameplay must be fully self-contained in published_manifest.
    After publishing, deliberately mutate or delete underlying Question and RoundQuestion
    in the database; verify that public gameplay still works 100% identically without
    relying on mutable DB rows.
    """
    # 1. Create and publish a quiz
    res = client.post("/api/drafts", json={"title": "Immutable Published Quiz"})
    draft_id = res.json()["version_id"]
    quiz_id = res.json()["quiz_id"]

    r = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "aldama_meni"}).json()
    round_id = r["round_id"]

    q_res = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
        "text": "O'zbekiston dengizga to'g'ridan-to'g'ri chiqish yo'liga egami?",
        "primary_answer": "Yo'q",
        "accepted_answers": ["Yoq", "No"],
        "correct_boolean": False,
        "explanation": "O'zbekiston ikkita davlat orqali dengizga chiqadigan ikki mamlakatdan biridir",
        "points": 1,
    }).json()
    q_id = q_res["question_id"]
    rq_id = q_res["round_question_id"]

    # Publish
    pub = client.post(f"/api/drafts/{draft_id}/publish")
    assert pub.status_code == status.HTTP_200_OK

    # 2. DELIBERATELY MUTATE AND CORRUPT DB Question and AcceptedAnswers
    db_q = db_session.query(Question).filter(Question.id == q_id).first()
    db_q.text = "MUTATED CORRUPTED TEXT"
    # Delete DB accepted answers
    db_session.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q_id).delete()
    # Add wrong accepted answer in DB
    db_session.add(AcceptedAnswer(question_id=q_id, answer_text="Ha", is_primary=True))
    db_session.commit()

    # 3. Start public gameplay session
    play_res = client.post(f"/api/play/start/{quiz_id}")
    assert play_res.status_code == status.HTTP_201_CREATED
    play_data = play_res.json()
    token = play_data["session_token"]

    # Verify the question text displayed to the player is the ORIGINAL frozen text from manifest, NOT the mutated DB text!
    assert play_data["current_state"]["question"]["text"] == "O'zbekiston dengizga to'g'ridan-to'g'ri chiqish yo'liga egami?"
    assert "MUTATED" not in play_data["current_state"]["question"]["text"]

    # 4. Submit the ORIGINAL correct answer ("yo'q")
    sub_res = client.post(f"/api/play/{token}/answer", json={
        "answer": "yo'q",
    })
    assert sub_res.status_code == status.HTTP_200_OK
    sub_data = sub_res.json()
    # It must evaluate as TRUE because it evaluated against the published_manifest accepted answers, NOT the corrupted DB answer ("Ha")!
    assert sub_data["is_correct"] is True
    assert sub_data["points_awarded"] == 1

    # 5. Reveal round answers: verify revealed answers and explanation are from the manifest!
    rev_res = client.get(f"/api/play/{token}/reveal")
    assert rev_res.status_code == status.HTTP_200_OK
    rev_data = rev_res.json()
    assert any("yo'q" in str(a).lower() for a in rev_data["questions"][0]["correct_answers"])
    assert "ikkita davlat" in rev_data["questions"][0]["explanation"]
