import pytest
from fastapi import status
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer


@pytest.fixture
def secure_quiz(db_session):
    quiz = Quiz(title="Security Test Quiz", description="Testing against data leaks")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published")
    db_session.add(version)
    db_session.flush()

    # Round 1: Mantiqasqon with hidden rule
    r1 = Round(
        quiz_version_id=version.id,
        sequence=1,
        round_type="mantiqasqon",
        config={"hidden_rule": "TOP_SECRET_RULE", "theme": "General"}
    )
    db_session.add(r1)
    db_session.flush()

    q1 = Question(round_id=r1.id, sequence=1, text="1-savol matni?", points=10)
    db_session.add(q1)
    db_session.flush()
    db_session.add(AcceptedAnswer(question_id=q1.id, answer_text="SECRET_ANSWER_1", is_primary=True))

    q2 = Question(round_id=r1.id, sequence=2, text="2-savol matni?", points=10)
    db_session.add(q2)
    db_session.flush()
    db_session.add(AcceptedAnswer(question_id=q2.id, answer_text="SECRET_ANSWER_2", is_primary=True))

    db_session.commit()
    return quiz


def test_discovery_endpoint_does_not_leak(client, secure_quiz):
    """Quiz discovery must not expose hidden rules or accepted answers."""
    res = client.get(f"/api/quizzes/{secure_quiz.id}")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()

    # Hidden rule stripped
    assert "TOP_SECRET_RULE" not in str(data)
    # Answers stripped
    assert "SECRET_ANSWER_1" not in str(data)
    assert "SECRET_ANSWER_2" not in str(data)


def test_gameplay_states_do_not_leak_before_reveal(client, secure_quiz):
    """Start, current_state, and answer payloads must not expose hidden answers or rules."""
    # 1. Start solo attempt
    start_res = client.post(f"/api/play/start/{secure_quiz.id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    start_data = start_res.json()
    token = start_data["session_token"]

    assert "SECRET_ANSWER_1" not in str(start_data)
    assert "SECRET_ANSWER_2" not in str(start_data)
    assert "TOP_SECRET_RULE" not in str(start_data)

    # 2. Get current state
    state_res = client.get(f"/api/play/{token}")
    assert state_res.status_code == status.HTTP_200_OK
    assert "SECRET_ANSWER_1" not in str(state_res.json())
    assert "SECRET_ANSWER_2" not in str(state_res.json())
    assert "TOP_SECRET_RULE" not in str(state_res.json())

    # 3. Premature reveal attempt MUST return 400 Bad Request
    premature_reveal = client.get(f"/api/play/{token}/reveal")
    assert premature_reveal.status_code == status.HTTP_400_BAD_REQUEST

    # 4. Submit first answer
    ans1_res = client.post(f"/api/play/{token}/answer", json={"answer": "SECRET_ANSWER_1"})
    assert ans1_res.status_code == status.HTTP_200_OK
    ans1_data = ans1_res.json()
    assert ans1_data["is_correct"] is True
    assert "SECRET_ANSWER_2" not in str(ans1_data)
    assert "TOP_SECRET_RULE" not in str(ans1_data)

    # Still in progress (Q2 remaining), premature reveal must still fail
    premature_reveal2 = client.get(f"/api/play/{token}/reveal")
    assert premature_reveal2.status_code == status.HTTP_400_BAD_REQUEST

    # 5. Submit second answer (round finishes)
    ans2_res = client.post(f"/api/play/{token}/answer", json={"answer": "wrong_guess"})
    assert ans2_res.status_code == status.HTTP_200_OK
    ans2_data = ans2_res.json()
    assert ans2_data["round_completed"] is True
    assert ans2_data["reveal_available"] is True
    assert ans2_data["next_state"] is None

    # 6. Now reveal is legitimately available
    reveal_res = client.get(f"/api/play/{token}/reveal")
    assert reveal_res.status_code == status.HTTP_200_OK
    reveal_data = reveal_res.json()

    # Now the rule and correct answers are revealed for review
    assert reveal_data["config"]["hidden_rule"] == "TOP_SECRET_RULE"
    assert "SECRET_ANSWER_1" in str(reveal_data["questions"])
    assert "SECRET_ANSWER_2" in str(reveal_data["questions"])
