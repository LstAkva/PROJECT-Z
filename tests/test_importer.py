import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, Question, AcceptedAnswer, Quiz, Round
from importer import import_questions


@pytest.fixture
def mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def sample_json_file(tmp_path):
    data = [
        {
            "id": "tg_q1",
            "text": "Telegram xabaridagi 1-savol?",
            "primary_answer": "Birinchi",
            "accepted_answers": [{"text": "Birinchi", "type": "primary"}],
            "points": 10,
            "source": {
                "channel_id": "999888",
                "question_message_id": 42,
                "source_name": "Test Channel"
            },
            "editorial": {"status": "ready", "flags": []}
        },
        {
            "id": "tg_q2",
            "text": "Telegram xabaridagi 2-savol (aynan o'sha xabarda)?",
            "primary_answer": "Ikkinchi",
            "accepted_answers": [{"text": "Ikkinchi", "type": "primary"}],
            "points": 20,
            "source": {
                "channel_id": "999888",
                "question_message_id": 42,
                "source_name": "Test Channel"
            },
            "editorial": {"status": "ready", "flags": []}
        },
        {
            "id": "tg_q3",
            "text": "Boshqa xabardagi 3-savol?",
            "primary_answer": "Uchinchi",
            "accepted_answers": [{"text": "Uchinchi", "type": "primary"}],
            "points": 30,
            "source": {
                "channel_id": "999888",
                "question_message_id": 43,
                "source_name": "Test Channel"
            },
            "editorial": {"status": "ready", "flags": []}
        }
    ]
    file_path = tmp_path / "test_canonical.json"
    file_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(file_path)


def test_dry_run_performs_zero_writes(mem_db, sample_json_file):
    """Verify that --dry-run produces 0 committed database rows."""
    stats = import_questions(sample_json_file, dry_run=True, db_session=mem_db)
    assert stats["total"] == 3
    assert stats["added"] == 3

    # Check that database has 0 rows because transaction was rolled back
    q_count = mem_db.query(Question).count()
    ans_count = mem_db.query(AcceptedAnswer).count()
    assert q_count == 0
    assert ans_count == 0


def test_importer_respects_limit(mem_db, sample_json_file):
    """Verify that --limit processes exactly the requested number of records."""
    stats = import_questions(sample_json_file, limit=2, dry_run=False, db_session=mem_db)
    assert stats["total"] == 2
    assert stats["added"] == 2

    q_count = mem_db.query(Question).count()
    assert q_count == 2


def test_telegram_multi_question_not_collapsed(mem_db, sample_json_file):
    """
    Verify that multiple questions originating from the same Telegram message
    (channel_id=999888, message_id=42) are both imported and not collapsed as duplicates.
    """
    stats = import_questions(sample_json_file, dry_run=False, db_session=mem_db)
    assert stats["added"] == 3
    assert stats["skipped_duplicate"] == 0

    questions = mem_db.query(Question).all()
    assert len(questions) == 3

    # Both questions from message 42 exist
    msg_42_questions = [
        q for q in questions
        if q.source_meta.get("question_message_id") == 42
    ]
    assert len(msg_42_questions) == 2


def test_duplicate_reimport_skipped(mem_db, sample_json_file):
    """Verify that importing the exact same file twice flags all records as duplicates."""
    stats1 = import_questions(sample_json_file, dry_run=False, db_session=mem_db)
    assert stats1["added"] == 3

    stats2 = import_questions(sample_json_file, dry_run=False, db_session=mem_db)
    assert stats2["added"] == 0
    assert stats2["skipped_duplicate"] == 3


def test_canonical_id_deduplication(mem_db, tmp_path):
    """Verify that records with identical canonical_id are recognized as duplicates."""
    item1 = {
        "id": "unique_id_100",
        "text": "Savol 1 matni?",
        "primary_answer": "Javob 1",
        "accepted_answers": [{"text": "Javob 1", "type": "primary"}],
        "points": 10,
        "source": {"source_name": "Test", "source_file": "f1", "question_number": 1},
        "editorial": {"status": "ready"}
    }
    item2 = {
        "id": "unique_id_100",  # Same canonical_id
        "text": "O'zgargan savol matni?",
        "primary_answer": "Boshqa javob",
        "accepted_answers": [{"text": "Boshqa javob", "type": "primary"}],
        "points": 10,
        "source": {"source_name": "Test", "source_file": "f2", "question_number": 2},
        "editorial": {"status": "ready"}
    }

    f1 = tmp_path / "f1.json"
    f1.write_text(json.dumps([item1], ensure_ascii=False), encoding="utf-8")
    f2 = tmp_path / "f2.json"
    f2.write_text(json.dumps([item2], ensure_ascii=False), encoding="utf-8")

    stats1 = import_questions(str(f1), dry_run=False, db_session=mem_db)
    assert stats1["added"] == 1

    stats2 = import_questions(str(f2), dry_run=False, db_session=mem_db)
    assert stats2["added"] == 0
    assert stats2["skipped_duplicate"] == 1


def test_canonical_record_with_editorial_needs_review_becomes_ready(mem_db, tmp_path):
    """
    Contract 1: A canonical record with editorial.status = 'needs_review'
    AND QG result = 'ready_to_import' MUST be imported as Question.status = 'ready'.
    """
    record = {
        "id": "qg_ready_ed_review_1",
        "text": "O'zbekiston poytaxti qaysi shahar?",
        "primary_answer": "Toshkent",
        "accepted_answers": [{"text": "Toshkent", "type": "primary"}],
        "points": 10,
        "source": {
            "channel_id": "123456",
            "question_message_id": 10,
            "source_name": "Test Source"
        },
        # Raw source contains editorial.status = 'needs_review'
        "editorial": {"status": "needs_review", "flags": []}
    }
    f = tmp_path / "test_status.json"
    f.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")

    stats = import_questions(str(f), dry_run=False, db_session=mem_db)
    assert stats["added"] == 1
    assert stats["ready"] == 1
    assert stats["needs_review"] == 0
    assert stats["skipped_needs_review"] == 0
    assert stats["skipped_rejected"] == 0

    q = mem_db.query(Question).filter(Question.text == record["text"]).first()
    assert q is not None
    assert q.status == "ready", f"Expected Question.status == 'ready', got '{q.status}'"


def test_qg_needs_review_skipped_by_default_and_admitted_when_allowed(mem_db, tmp_path):
    """
    Contract 2: A QG 'needs_review' record must be skipped by default,
    and imported as status='needs_review' when allow_needs_review=True.
    """
    # Answer with brackets triggers QG 'needs_review'
    record = {
        "id": "qg_review_1",
        "text": "Mashhur tennischi kim?",
        "primary_answer": "(Jon) Isner",
        "accepted_answers": [{"text": "(Jon) Isner", "type": "primary"}],
        "points": 20,
        "source": {
            "channel_id": "123456",
            "question_message_id": 20,
            "source_name": "Test Source"
        },
        "editorial": {"status": "ready", "flags": []}
    }
    f = tmp_path / "test_review.json"
    f.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")

    # Default import: skipped
    stats_default = import_questions(str(f), dry_run=False, allow_needs_review=False, db_session=mem_db)
    assert stats_default["added"] == 0
    assert stats_default["skipped_needs_review"] == 1
    assert mem_db.query(Question).count() == 0

    # Allowed import: admitted with status='needs_review'
    stats_allowed = import_questions(str(f), dry_run=False, allow_needs_review=True, db_session=mem_db)
    assert stats_allowed["added"] == 1
    assert stats_allowed["needs_review"] == 1
    assert stats_allowed["ready"] == 0

    q = mem_db.query(Question).first()
    assert q is not None
    assert q.status == "needs_review"


def test_qg_rejected_record_skipped(mem_db, tmp_path):
    """
    Contract 3: A record that fails Quality Gate (rejected) MUST NOT be imported.
    """
    record = {
        "id": "qg_rejected_1",
        "text": "Rossiya poytaxti qaysi shahar?",
        "primary_answer": "Москва",  # Cyrillic triggers rejection
        "accepted_answers": [{"text": "Москва", "type": "primary"}],
        "points": 10,
        "source": {
            "channel_id": "123456",
            "question_message_id": 30,
            "source_name": "Test Source"
        },
        "editorial": {"status": "ready", "flags": []}
    }
    f = tmp_path / "test_reject.json"
    f.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")

    stats = import_questions(str(f), dry_run=False, db_session=mem_db)
    assert stats["added"] == 0
    assert stats["skipped_rejected"] == 1
    assert mem_db.query(Question).count() == 0


def test_no_dummy_quiz_or_round_created(mem_db, sample_json_file):
    """
    Contract 6: Importing into Question Bank creates zero dummy Quiz, QuizVersion, or Round rows.
    Questions must have round_id IS NULL.
    """
    stats = import_questions(sample_json_file, dry_run=False, db_session=mem_db)
    assert stats["added"] == 3

    assert mem_db.query(Quiz).count() == 0
    assert mem_db.query(Round).count() == 0

    questions = mem_db.query(Question).all()
    assert len(questions) == 3
    for q in questions:
        assert q.round_id is None


def test_dev_database_not_touched_by_tests(mem_db):
    """
    Contract 5: Importer tests use an isolated SQLite in-memory session fixture (mem_db),
    guaranteeing that the remote database is never modified during test execution.
    """
    assert str(mem_db.bind.url).startswith("sqlite://"), "Test database must be isolated in-memory SQLite"
    assert mem_db.bind.name == "sqlite"


