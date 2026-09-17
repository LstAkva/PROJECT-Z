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
