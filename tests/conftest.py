import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer
from api.quizzes import compile_published_manifest

# Isolated in-memory SQLite database for testing
TEST_DATABASE_URL = "sqlite:///:memory:"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def setup_test_database():
    """Recreates schema before every test function for pristine isolation."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session():
    """Provides a fresh transactional session for test assertions/seeding."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    """FastAPI TestClient with overridden get_db dependency."""
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seed_sample_quiz(db_session):
    """
    Seeds a canonical 4-round published quiz using Question Bank + RoundQuestion architecture:
    1. standard
    2. true_false
    3. mantiqasqon
    4. zanjir
    """
    quiz = Quiz(title="ZakoWhat Test Championship", description="Automated verification quiz.")
    db_session.add(quiz)
    db_session.flush()

    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="modern_multiround")
    db_session.add(version)
    db_session.flush()

    # Round 1: Standard
    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db_session.add(r1)
    db_session.flush()
    q1 = Question(text="O'zbekiston poytaxti qaysi shahar?", points=1, default_points=1, status="ready")
    db_session.add(q1)
    db_session.flush()
    db_session.add(RoundQuestion(round_id=r1.id, question_id=q1.id, sequence=1, points_override=1))
    db_session.add_all([
        AcceptedAnswer(question_id=q1.id, answer_text="Toshkent", is_primary=True),
        AcceptedAnswer(question_id=q1.id, answer_text="Toshkent shahri", is_primary=False),
    ])

    # Round 2: True/False
    r2 = Round(quiz_version_id=version.id, sequence=2, round_type="true_false")
    db_session.add(r2)
    db_session.flush()
    q2 = Question(text="Yer quyosh atrofida aylanadi.", points=1, default_points=1, status="ready", question_type="true_false")
    db_session.add(q2)
    db_session.flush()
    db_session.add(RoundQuestion(round_id=r2.id, question_id=q2.id, sequence=1, points_override=1))
    db_session.add(AcceptedAnswer(question_id=q2.id, answer_text="rost", is_primary=True))

    # Round 3: Mantiqasqon
    r3 = Round(
        quiz_version_id=version.id,
        sequence=3,
        round_type="mantiqasqon",
        config={"hidden_rule": "Barcha javoblar mevalar nomlari"}
    )
    db_session.add(r3)
    db_session.flush()
    q3 = Question(text="Qizil rangli shirin meva", points=2, default_points=2, status="ready")
    q4 = Question(text="Sariq rangli nordon sitrus mevasi", points=2, default_points=2, status="ready")
    db_session.add_all([q3, q4])
    db_session.flush()
    db_session.add(RoundQuestion(round_id=r3.id, question_id=q3.id, sequence=1, points_override=2))
    db_session.add(RoundQuestion(round_id=r3.id, question_id=q4.id, sequence=2, points_override=2))
    db_session.add(AcceptedAnswer(question_id=q3.id, answer_text="olma", is_primary=True))
    db_session.add(AcceptedAnswer(question_id=q4.id, answer_text="limon", is_primary=True))

    # Round 4: Zanjir
    r4 = Round(quiz_version_id=version.id, sequence=4, round_type="zanjir")
    db_session.add(r4)
    db_session.flush()
    # Chain: quyosh (ends in sh) -> shahar (ends in r) -> rishton (ends in n)
    q5 = Question(text="Kunduzgi yorug'lik manbai", points=1, default_points=1, status="ready")
    q6 = Question(text="Aholi zich yashaydigan ma'muriy markaz", points=1, default_points=1, status="ready")
    q7 = Question(text="Farg'ona vodiysidagi kulolchilik shahri", points=1, default_points=1, status="ready")
    db_session.add_all([q5, q6, q7])
    db_session.flush()
    db_session.add(RoundQuestion(round_id=r4.id, question_id=q5.id, sequence=1, points_override=1))
    db_session.add(RoundQuestion(round_id=r4.id, question_id=q6.id, sequence=2, points_override=1))
    db_session.add(RoundQuestion(round_id=r4.id, question_id=q7.id, sequence=3, points_override=1))
    db_session.add_all([
        AcceptedAnswer(question_id=q5.id, answer_text="quyosh", is_primary=True),
        AcceptedAnswer(question_id=q6.id, answer_text="shahar", is_primary=True),
        AcceptedAnswer(question_id=q7.id, answer_text="rishton", is_primary=True),
    ])

    # Attach compiled immutable published_manifest
    version.published_manifest = compile_published_manifest(version, db_session)
    db_session.commit()
    return quiz
