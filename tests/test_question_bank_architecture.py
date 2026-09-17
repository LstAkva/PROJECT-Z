import json
import pytest
from fastapi import status
from sqlalchemy.exc import IntegrityError
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, AnswerRecord
from importer import import_questions
from api.quizzes import compile_published_manifest


def test_question_can_be_reused_across_multiple_rounds(db_session):
    """Verify that a single Question in the Question Bank can be placed in multiple rounds."""
    quiz = Quiz(title="Multi-Round Quiz")
    db_session.add(quiz)
    db_session.flush()

    v1 = QuizVersion(quiz_id=quiz.id, version_number=1, status="draft")
    db_session.add(v1)
    db_session.flush()

    r1 = Round(quiz_version_id=v1.id, sequence=1, round_type="standard")
    r2 = Round(quiz_version_id=v1.id, sequence=2, round_type="vabank")
    db_session.add_all([r1, r2])
    db_session.flush()

    # Create one single Question in the Question Bank
    q = Question(text="Alisher Navoiy qachon tug'ilgan?", default_points=1, points=1, status="ready")
    db_session.add(q)
    db_session.flush()

    # Reuse the question in Round 1 (sequence 1, 1 pt) and Round 2 (sequence 1, 2 pts)
    rq1 = RoundQuestion(round_id=r1.id, question_id=q.id, sequence=1, points_override=1)
    rq2 = RoundQuestion(round_id=r2.id, question_id=q.id, sequence=1, points_override=2)
    db_session.add_all([rq1, rq2])
    db_session.commit()

    assert db_session.query(Question).count() == 1
    assert db_session.query(RoundQuestion).count() == 2
    assert rq1.question.id == rq2.question.id == q.id
    assert rq1.points_override == 1
    assert rq2.points_override == 2


def test_same_question_cannot_appear_twice_in_one_round(db_session):
    """Verify that UNIQUE(round_id, question_id) prevents repeating a question within the same round."""
    quiz = Quiz(title="Duplicate Test Quiz")
    db_session.add(quiz)
    db_session.flush()

    v1 = QuizVersion(quiz_id=quiz.id, version_number=1, status="draft")
    db_session.add(v1)
    db_session.flush()

    r1 = Round(quiz_version_id=v1.id, sequence=1, round_type="standard")
    db_session.add(r1)
    db_session.flush()

    q = Question(text="Qaysi daryo O'zbekistondan oqib o'tadi?", status="ready")
    db_session.add(q)
    db_session.flush()

    rq1 = RoundQuestion(round_id=r1.id, question_id=q.id, sequence=1)
    db_session.add(rq1)
    db_session.commit()

    # Attempt to add same question to same round at sequence 2
    rq2 = RoundQuestion(round_id=r1.id, question_id=q.id, sequence=2)
    db_session.add(rq2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_sequence_uniqueness_within_round(db_session):
    """Verify that UNIQUE(round_id, sequence) prevents two questions at the same ordinal position."""
    quiz = Quiz(title="Sequence Test Quiz")
    db_session.add(quiz)
    db_session.flush()

    v1 = QuizVersion(quiz_id=quiz.id, version_number=1, status="draft")
    db_session.add(v1)
    db_session.flush()

    r1 = Round(quiz_version_id=v1.id, sequence=1, round_type="standard")
    db_session.add(r1)
    db_session.flush()

    q1 = Question(text="Savol 1?", status="ready")
    q2 = Question(text="Savol 2?", status="ready")
    db_session.add_all([q1, q2])
    db_session.flush()

    rq1 = RoundQuestion(round_id=r1.id, question_id=q1.id, sequence=1)
    db_session.add(rq1)
    db_session.commit()

    # Attempt to place q2 at sequence 1 in the same round
    rq2 = RoundQuestion(round_id=r1.id, question_id=q2.id, sequence=1)
    db_session.add(rq2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_points_and_config_overrides(db_session):
    """Verify that points_override and config_override properly supersede Question defaults."""
    quiz = Quiz(title="Override Test Quiz")
    db_session.add(quiz)
    db_session.flush()

    v1 = QuizVersion(quiz_id=quiz.id, version_number=1, status="draft")
    db_session.add(v1)
    db_session.flush()

    r1 = Round(quiz_version_id=v1.id, sequence=1, round_type="standard")
    db_session.add(r1)
    db_session.flush()

    q = Question(text="Asosiy savol", default_points=1, points=1, status="ready")
    db_session.add(q)
    db_session.flush()

    rq = RoundQuestion(
        round_id=r1.id,
        question_id=q.id,
        sequence=1,
        points_override=10,
        config_override={"time_limit_sec": 45}
    )
    db_session.add(rq)
    db_session.commit()

    manifest = compile_published_manifest(v1, db_session)
    q_data = manifest["rounds"][0]["questions"][0]
    assert q_data["points"] == 10
    assert q_data["config"]["time_limit_sec"] == 45


def test_importer_inserts_without_fake_quiz_or_round(db_session, tmp_path):
    """Verify that canonical importer writes directly to Question Bank without dummy entities."""
    data = [
        {
            "id": "direct_qb_1",
            "text": "To'g'ridan-to'g'ri bankka kiritiluvchi savol?",
            "primary_answer": "Javob 1",
            "accepted_answers": [{"text": "Javob 1", "type": "primary"}],
            "points": 5,
            "source": {"source_name": "Unit Test", "source_file": "qb_test.json", "question_number": 1},
            "editorial": {"status": "ready"}
        }
    ]
    file_path = tmp_path / "qb_test.json"
    file_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    stats = import_questions(str(file_path), dry_run=False, db_session=db_session)
    assert stats["added"] == 1

    # Questions and AcceptedAnswers exist
    assert db_session.query(Question).count() == 1
    assert db_session.query(AcceptedAnswer).count() == 1

    # NO fake Quiz and NO fake Round exist
    assert db_session.query(Quiz).count() == 0
    assert db_session.query(Round).count() == 0

    q = db_session.query(Question).first()
    assert q.round_id is None
    assert q.sequence is None
    assert q.default_points == 5


def test_mcq_answer_evaluation_and_leak_prevention(client, db_session):
    """Verify 'Gulmisiz, rayhonmisiz?' MCQ ABCD validation and safe option payload delivery."""
    quiz = Quiz(title="MCQ Championship")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="modern_multiround")
    db_session.add(version)
    db_session.flush()

    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="gulmisiz")
    db_session.add(r1)
    db_session.flush()

    options = [
        {"key": "A", "text": "Alisher Navoiy"},
        {"key": "B", "text": "Zahiriddin Muhammad Bobur"},
        {"key": "C", "text": "Nizomiy Ganjaviy"},
        {"key": "D", "text": "Amir Temur"}
    ]
    q = Question(
        text="Xamsa asari muallifi kim?",
        options=options,
        question_type="mcq",
        status="ready",
        points=1
    )
    db_session.add(q)
    db_session.flush()

    db_session.add(RoundQuestion(round_id=r1.id, question_id=q.id, sequence=1))
    db_session.add(AcceptedAnswer(question_id=q.id, answer_text="A", is_primary=True))
    version.published_manifest = compile_published_manifest(version, db_session)
    db_session.commit()

    # 1. Start Attempt
    resp = client.post(f"/api/play/start/{quiz.id}")
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    token = data["session_token"]
    q_state = data["current_state"]["question"]

    # Verify options are delivered
    assert q_state["question_type"] == "mcq"
    assert len(q_state["options"]) == 4
    assert q_state["options"][0]["key"] == "A"
    assert q_state["options"][0]["text"] == "Alisher Navoiy"

    # CRITICAL: Verify correct answer is NOT leaked
    assert "accepted_answers" not in q_state
    assert "correct_answers" not in q_state
    assert "A" not in str(data["current_state"]["round"]["config"])

    # 2. Submit correct option key "A"
    ans_resp = client.post(f"/api/play/{token}/answer", json={"answer": "A"})
    assert ans_resp.status_code == status.HTTP_200_OK
    assert ans_resp.json()["is_correct"] is True
    assert ans_resp.json()["points_awarded"] == 1

    # 3. Call /reveal
    rev_resp = client.get(f"/api/play/{token}/reveal")
    assert rev_resp.status_code == status.HTTP_200_OK
    rev_q = rev_resp.json()["questions"][0]
    assert rev_q["is_correct"] is True
    assert rev_q["correct_answers"] == ["A"]


def test_vabank_scoring_rules(client, db_session):
    """Verify Vabank rules: normal (+1/-1), wager (+2/-2), and blank (0)."""
    quiz = Quiz(title="Vabank Tournament")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published")
    db_session.add(version)
    db_session.flush()

    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="vabank")
    db_session.add(r1)
    db_session.flush()

    # 5 questions to test: normal correct, normal wrong, wager correct, wager wrong, blank
    questions = []
    for i in range(1, 6):
        q = Question(text=f"Vabank Savol {i}", points=1, status="ready")
        db_session.add(q)
        db_session.flush()
        db_session.add(RoundQuestion(round_id=r1.id, question_id=q.id, sequence=i))
        db_session.add(AcceptedAnswer(question_id=q.id, answer_text=f"javob{i}", is_primary=True))
        questions.append(q)

    version.published_manifest = compile_published_manifest(version, db_session)
    db_session.commit()

    start_resp = client.post(f"/api/play/start/{quiz.id}")
    token = start_resp.json()["session_token"]

    # Q1: Normal correct -> +1
    res1 = client.post(f"/api/play/{token}/answer", json={"answer": "javob1", "is_wager": False})
    assert res1.json()["is_correct"] is True
    assert res1.json()["points_awarded"] == 1
    assert res1.json()["total_score"] == 1

    # Q2: Normal wrong -> -1
    res2 = client.post(f"/api/play/{token}/answer", json={"answer": "noto'g'ri", "is_wager": False})
    assert res2.json()["is_correct"] is False
    assert res2.json()["points_awarded"] == -1
    assert res2.json()["total_score"] == 0

    # Q3: Wager correct -> +2
    res3 = client.post(f"/api/play/{token}/answer", json={"answer": "javob3", "is_wager": True})
    assert res3.json()["is_correct"] is True
    assert res3.json()["points_awarded"] == 2
    assert res3.json()["total_score"] == 2

    # Q4: Wager wrong via leading '+' -> -2
    res4 = client.post(f"/api/play/{token}/answer", json={"answer": "+xato"})
    assert res4.json()["is_correct"] is False
    assert res4.json()["points_awarded"] == -2
    assert res4.json()["total_score"] == 0

    # Q5: Blank / pass -> 0 points (no penalty!)
    res5 = client.post(f"/api/play/{token}/answer", json={"answer": ""})
    assert res5.json()["is_correct"] is False
    assert res5.json()["points_awarded"] == 0
    assert res5.json()["total_score"] == 0


def test_classic_zakovat_12_plus_12_progression(client, db_session):
    """Verify Classic Zakovat mode: 2 tours of 12 questions with intermediate reveal and final tally."""
    quiz = Quiz(title="Zakovat Chempionati")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="classic_zakovat")
    db_session.add(version)
    db_session.flush()

    # Tour 1 (12 Qs) and Tour 2 (12 Qs)
    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="zakovat_tour", config={"tour": 1})
    r2 = Round(quiz_version_id=version.id, sequence=2, round_type="zakovat_tour", config={"tour": 2})
    db_session.add_all([r1, r2])
    db_session.flush()

    # Create 24 questions
    for tour_round in [r1, r2]:
        for seq in range(1, 13):
            q = Question(text=f"Zakovat Tour {tour_round.sequence} Q{seq}", points=1, status="ready")
            db_session.add(q)
            db_session.flush()
            db_session.add(RoundQuestion(round_id=tour_round.id, question_id=q.id, sequence=seq))
            db_session.add(AcceptedAnswer(question_id=q.id, answer_text=f"ans_{tour_round.sequence}_{seq}", is_primary=True))

    version.published_manifest = compile_published_manifest(version, db_session)
    db_session.commit()

    # Start attempt
    resp = client.post(f"/api/play/start/{quiz.id}")
    assert resp.status_code == status.HTTP_201_CREATED
    token = resp.json()["session_token"]
    assert resp.json()["game_mode"] == "classic_zakovat"

    # Play Tour 1 (12 questions)
    for seq in range(1, 13):
        ans_resp = client.post(f"/api/play/{token}/answer", json={"answer": f"ans_1_{seq}"})
        assert ans_resp.status_code == status.HTTP_200_OK

    # Verify Tour 1 finished and entered round_reveal
    state_resp = client.get(f"/api/play/{token}")
    assert state_resp.json()["status"] == "round_reveal"

    # Intermediate Tour 1 reveal
    rev_resp = client.get(f"/api/play/{token}/reveal")
    assert rev_resp.status_code == status.HTTP_200_OK
    assert rev_resp.json()["round_score"] == 12

    # Continue to Tour 2
    cont_resp = client.post(f"/api/play/{token}/continue")
    assert cont_resp.status_code == status.HTTP_200_OK
    assert cont_resp.json()["current_state"]["round"]["sequence"] == 2

    # Play Tour 2 (12 questions: 10 correct, 2 wrong)
    for seq in range(1, 13):
        ans = f"ans_2_{seq}" if seq <= 10 else "xato"
        client.post(f"/api/play/{token}/answer", json={"answer": ans})

    # Tour 2 complete
    rev2_resp = client.get(f"/api/play/{token}/reveal")
    assert rev2_resp.status_code == status.HTTP_200_OK
    assert rev2_resp.json()["round_score"] == 10

    fin_cont = client.post(f"/api/play/{token}/continue")
    assert fin_cont.json()["status"] == "completed"

    # Final results
    results_resp = client.get(f"/api/play/{token}/results")
    assert results_resp.status_code == status.HTTP_200_OK
    data = results_resp.json()
    assert data["game_mode"] == "classic_zakovat"
    assert data["total_score"] == 22
    assert data["total_correct"] == 22
    assert data["total_incorrect"] == 2
    assert len(data["rounds"]) == 2
    assert data["rounds"][0]["round_score"] == 12
    assert data["rounds"][1]["round_score"] == 10


def test_published_manifest_immutability(client, db_session):
    """Verify that editing the Question Bank AFTER publishing does NOT modify the published gameplay."""
    quiz = Quiz(title="Immutability Quiz")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published")
    db_session.add(version)
    db_session.flush()

    r = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db_session.add(r)
    db_session.flush()

    q = Question(text="Original Savol Matni?", points=1, default_points=1, status="ready")
    db_session.add(q)
    db_session.flush()

    db_session.add(RoundQuestion(round_id=r.id, question_id=q.id, sequence=1))
    db_session.add(AcceptedAnswer(question_id=q.id, answer_text="OriginalJavob", is_primary=True))

    version.published_manifest = compile_published_manifest(version, db_session)
    db_session.commit()

    # Start player session
    start_resp = client.post(f"/api/play/start/{quiz.id}")
    token = start_resp.json()["session_token"]
    assert start_resp.json()["current_state"]["question"]["text"] == "Original Savol Matni?"

    # NOW: Malicious or accidental edit to Question Bank directly
    q.text = "MUTATED / HACKED QUESTION TEXT"
    # Change accepted answer in Question Bank
    ans = db_session.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q.id).first()
    ans.answer_text = "HackedJavob"
    db_session.commit()

    # Verify gameplay STILL receives original question from published_manifest
    curr_resp = client.get(f"/api/play/{token}")
    assert curr_resp.json()["question"]["text"] == "Original Savol Matni?"

    # Verify original answer is accepted and hacked answer is NOT accepted
    hacked_ans = client.post(f"/api/play/{token}/answer", json={"answer": "HackedJavob"})
    assert hacked_ans.json()["is_correct"] is False

    # Start new attempt on same published quiz
    new_start = client.post(f"/api/play/start/{quiz.id}")
    new_token = new_start.json()["session_token"]
    orig_ans = client.post(f"/api/play/{new_token}/answer", json={"answer": "OriginalJavob"})
    assert orig_ans.json()["is_correct"] is True


def test_svoyak_negative_scoring(client, db_session):
    """Verify Svoяk mode: tiered points, correct awards +points, wrong deducts -points, blank gives 0."""
    quiz = Quiz(title="Svoyak Game")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="svoyak")
    db_session.add(version)
    db_session.flush()

    r = Round(quiz_version_id=version.id, sequence=1, round_type="svoyak_theme", config={"theme": "Adabiyot"})
    db_session.add(r)
    db_session.flush()

    # 3 questions with tiered points: 10, 20, 30
    for i, pts in enumerate([10, 20, 30], start=1):
        q = Question(text=f"Svoyak Savol {i}", points=pts, default_points=pts, status="ready")
        db_session.add(q)
        db_session.flush()
        db_session.add(RoundQuestion(round_id=r.id, question_id=q.id, sequence=i, points_override=pts))
        db_session.add(AcceptedAnswer(question_id=q.id, answer_text=f"ans{i}", is_primary=True))

    version.published_manifest = compile_published_manifest(version, db_session)
    db_session.commit()

    start_resp = client.post(f"/api/play/start/{quiz.id}")
    token = start_resp.json()["session_token"]

    # Q1 (10 pts): Correct -> +10
    ans1 = client.post(f"/api/play/{token}/answer", json={"answer": "ans1"})
    assert ans1.json()["points_awarded"] == 10
    assert ans1.json()["total_score"] == 10

    # Q2 (20 pts): Wrong -> -20
    ans2 = client.post(f"/api/play/{token}/answer", json={"answer": "wrong"})
    assert ans2.json()["points_awarded"] == -20
    assert ans2.json()["total_score"] == -10

    # Q3 (30 pts): Blank -> 0
    ans3 = client.post(f"/api/play/{token}/answer", json={"answer": ""})
    assert ans3.json()["points_awarded"] == 0
    assert ans3.json()["total_score"] == -10


def test_old_answer_records_remain_valid(db_session):
    """Verify that AnswerRecords remain valid and resolve relationships smoothly."""
    quiz = Quiz(title="Legacy Quiz")
    db_session.add(quiz)
    db_session.flush()

    v = QuizVersion(quiz_id=quiz.id, version_number=1)
    db_session.add(v)
    db_session.flush()

    r = Round(quiz_version_id=v.id, sequence=1, round_type="standard")
    db_session.add(r)
    db_session.flush()

    q = Question(text="Legacy Question", default_points=1, points=1)
    db_session.add(q)
    db_session.flush()

    rq = RoundQuestion(round_id=r.id, question_id=q.id, sequence=1)
    db_session.add(rq)
    db_session.flush()

    attempt = SoloAttempt(quiz_version_id=v.id, total_score=1)
    db_session.add(attempt)
    db_session.flush()

    record = AnswerRecord(
        attempt_id=attempt.id,
        question_id=q.id,
        round_question_id=rq.id,
        submitted_text="Legacy Answer",
        is_correct=True,
        points_awarded=1,
    )
    db_session.add(record)
    db_session.commit()

    loaded_rec = db_session.query(AnswerRecord).filter(AnswerRecord.id == record.id).first()
    assert loaded_rec is not None
    assert loaded_rec.question.text == "Legacy Question"
    assert loaded_rec.round_question.sequence == 1
    assert loaded_rec.attempt.total_score == 1


def test_importer_content_hash_deduplication(db_session, tmp_path):
    """Verify that questions with matching normalized text and primary answer across files are deduped."""
    file1_data = [
        {
            "id": "file1_q1",
            "text": "O'zbekistonning poytaxti qaysi shahar?",
            "primary_answer": "Toshkent",
            "accepted_answers": [{"text": "Toshkent", "type": "primary"}],
            "points": 10,
            "source": {"source_name": "Source A", "source_file": "a.json", "question_number": 1},
            "editorial": {"status": "ready"}
        }
    ]
    file2_data = [
        {
            "id": "file2_q99",  # Different canonical ID
            "text": "O’zbekistonning poytaxti qaysi shahar?",  # Typographically different apostrophe
            "primary_answer": "Toshkent",
            "accepted_answers": [{"text": "Toshkent", "type": "primary"}],
            "points": 10,
            "source": {"source_name": "Source B", "source_file": "b.json", "question_number": 99},
            "editorial": {"status": "ready"}
        }
    ]
    f1 = tmp_path / "a.json"
    f2 = tmp_path / "b.json"
    f1.write_text(json.dumps(file1_data, ensure_ascii=False), encoding="utf-8")
    f2.write_text(json.dumps(file2_data, ensure_ascii=False), encoding="utf-8")

    stats1 = import_questions(str(f1), dry_run=False, db_session=db_session)
    assert stats1["added"] == 1

    # Second import should detect identical question despite different file/id and different apostrophe
    stats2 = import_questions(str(f2), dry_run=False, db_session=db_session)
    assert stats2["added"] == 0
    assert stats2["skipped_duplicate"] == 1
    assert db_session.query(Question).count() == 1

