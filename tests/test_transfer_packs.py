"""
Tests for scripts/transfer_packs.py (one-off dev -> production-branch pack transfer).

Source: a separate in-memory SQLite DB seeded with the 10 expected packs (+ test packs 86/87,
a draft, users and attempts that must NOT be copied).
Target: the regular test DB, pre-seeded with an existing published pack so that ids collide
and remapping is really exercised.
"""
import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import (
    AcceptedAnswer, AnswerRecord, Question, Quiz, QuizVersion, Round, RoundQuestion, SoloAttempt, User,
)
from api.quizzes import compile_published_manifest
from scripts import transfer_packs as tp

SRC = "ep-damp-frog-b1y9sc7x"


def _add_pack(db, quiz_id, title, game_mode, status="published"):
    quiz = Quiz(id=quiz_id, title=title, description=f"desc {quiz_id}")
    db.add(quiz)
    db.flush()
    v = QuizVersion(quiz_id=quiz.id, version_number=1, status=status, game_mode=game_mode)
    db.add(v)
    db.flush()
    if game_mode == "svoyak":
        round_specs = [("svoyak_theme", {"theme_title": f"Mavzu {i}"}, [10, 20]) for i in (1, 2)]
    else:
        round_specs = [("zakovat_classic", {"name": f"{i}-tur"}, [1, 1]) for i in (1, 2)]
    for seq, (rtype, cfg, points) in enumerate(round_specs, start=1):
        r = Round(quiz_version_id=v.id, sequence=seq, round_type=rtype, config=cfg)
        db.add(r)
        db.flush()
        for qseq, pts in enumerate(points, start=1):
            q = Question(text=f"Q{quiz_id}-{seq}-{qseq}", points=pts, default_points=pts, status="ready",
                         explanation=f"izoh {quiz_id}", source_meta={"origin": "dev"})
            db.add(q)
            db.flush()
            db.add(RoundQuestion(round_id=r.id, question_id=q.id, sequence=qseq, points_override=pts))
            db.add(AcceptedAnswer(question_id=q.id, answer_text=f"javob{quiz_id}{seq}{qseq}", is_primary=True))
            db.add(AcceptedAnswer(question_id=q.id, answer_text=f"alt{quiz_id}{seq}{qseq}", is_primary=False))
    db.flush()
    v.published_manifest = compile_published_manifest(v, db)
    v.published_manifest["category"] = "Zakovat" if game_mode != "svoyak" else "Svoya Igra"
    return quiz


@pytest.fixture
def source_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    for qid, title in tp.EXPECTED_PACKS.items():
        _add_pack(db, qid, title, "svoyak" if qid >= 81 else "classic_zakovat")
    _add_pack(db, 86, "Zakovat Restart Test 2", "classic_zakovat")
    _add_pack(db, 87, "Zakovat Restart Test Debug", "classic_zakovat")
    _add_pack(db, 48, "Classic Zakovat — Pack 06", "classic_zakovat", status="draft")
    user = User(email="dev@example.com", display_name="Dev")
    db.add(user)
    db.flush()
    db.add(SoloAttempt(quiz_version_id=43, user_id=user.id, session_token="dev-token", status="completed"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def target_db(db_session, seed_sample_quiz):
    return db_session


def test_transfer_copies_exactly_the_ten_packs_with_new_ids(source_db, target_db):
    before_quizzes = target_db.query(Quiz).count()
    report = tp.run_transfer(source_db, target_db, SRC)
    target_db.commit()

    assert report["ok"], report["validation_errors"]
    assert report["validation_errors"] == []
    assert sorted(i["source_quiz_id"] for i in report["copied"]) == sorted(tp.EXPECTED_PACKS)
    assert report["skipped"] == []
    assert target_db.query(Quiz).count() == before_quizzes + 10

    # Content deltas match the pack contents exactly; protected tables untouched.
    assert report["actual_delta"] == report["expected_delta"]
    assert report["actual_delta"]["quizzes"] == 10
    assert report["actual_delta"]["rounds"] == 20
    assert report["actual_delta"]["round_questions"] == 40
    assert report["actual_delta"]["questions"] == 40
    assert report["actual_delta"]["accepted_answers"] == 80
    for t in ("users", "solo_attempts", "answer_records", "daily_questions", "daily_submissions"):
        assert report["actual_delta"][t] == 0

    titles = {q.title for q in target_db.query(Quiz).all()}
    assert "Zakovat Restart Test 2" not in titles and "Zakovat Restart Test Debug" not in titles
    assert "Classic Zakovat — Pack 06" not in titles

    # Manifest ids rewritten to target rows (not source ids) and provenance recorded.
    for item in report["copied"]:
        v = target_db.get(QuizVersion, item["target_version_id"])
        m = v.published_manifest
        assert m["quiz_id"] == v.quiz_id and m["version_id"] == v.id
        assert m["transfer_provenance"]["source_quiz_id"] == item["source_quiz_id"]
        for rnd in m["rounds"]:
            assert target_db.get(Round, rnd["round_id"]).quiz_version_id == v.id
            for q in rnd["questions"]:
                rq = target_db.get(RoundQuestion, q["round_question_id"])
                assert rq.question_id == q["question_id"] and rq.round_id == rnd["round_id"]
                assert {a["id"] for a in q["accepted_answers"]} == {
                    a.id for a in target_db.query(AcceptedAnswer).filter_by(question_id=q["question_id"])
                }
                assert target_db.get(Question, q["question_id"]).source_meta["transfer_provenance"]["source_endpoint"] == SRC


def test_svoyak_points_and_accepted_answers_preserved(source_db, target_db):
    report = tp.run_transfer(source_db, target_db, SRC)
    target_db.commit()
    item = next(i for i in report["copied"] if i["source_quiz_id"] == 81)
    m = target_db.get(QuizVersion, item["target_version_id"]).published_manifest
    assert [[q["points"] for q in r["questions"]] for r in m["rounds"]] == [[10, 20], [10, 20]]
    first = m["rounds"][0]["questions"][0]
    assert [(a["answer_text"], a["is_primary"]) for a in first["accepted_answers"]] == [("javob8111", True), ("alt8111", False)]
    assert m["rounds"][0]["config"] == {"theme_title": "Mavzu 1"}


def test_second_run_is_idempotent(source_db, target_db):
    first = tp.run_transfer(source_db, target_db, SRC)
    target_db.commit()
    counts = tp.table_counts(target_db)

    second = tp.run_transfer(source_db, target_db, SRC)
    target_db.commit()
    assert second["ok"], second["validation_errors"]
    assert second["copied"] == []
    assert sorted(s["source_quiz_id"] for s in second["skipped"]) == sorted(tp.EXPECTED_PACKS)
    assert tp.table_counts(target_db) == counts
    assert first["ok"]


def test_dry_run_rollback_leaves_target_unchanged(source_db, target_db):
    counts = tp.table_counts(target_db)
    report = tp.run_transfer(source_db, target_db, SRC)
    assert report["ok"]
    target_db.rollback()
    assert tp.table_counts(target_db) == counts


def test_test_packs_are_refused(source_db, target_db):
    with pytest.raises(tp.TransferError, match="test pack"):
        tp.run_transfer(source_db, target_db, SRC, pack_ids=[43, 86])


def test_title_mismatch_is_refused(source_db, target_db):
    source_db.get(Quiz, 44).title = "Something else"
    source_db.flush()
    with pytest.raises(tp.TransferError, match="title mismatch"):
        tp.run_transfer(source_db, target_db, SRC)


def test_manifest_drift_in_source_is_refused(source_db, target_db):
    v = source_db.query(QuizVersion).filter_by(quiz_id=45).one()
    m = copy.deepcopy(v.published_manifest)
    m["rounds"][0]["questions"][0]["points"] = 99
    v.published_manifest = m
    source_db.flush()
    with pytest.raises(tp.TransferError, match="drift"):
        tp.run_transfer(source_db, target_db, SRC)


def test_transferred_pack_is_playable_and_records_target_ids(source_db, target_db, client):
    report = tp.run_transfer(source_db, target_db, SRC)
    target_db.commit()
    item = next(i for i in report["copied"] if i["source_quiz_id"] == 43)
    m = target_db.get(QuizVersion, item["target_version_id"]).published_manifest
    first_q = m["rounds"][0]["questions"][0]

    listing = client.get("/api/quizzes").json()
    assert item["target_quiz_id"] in {q["quiz_id"] for q in listing}

    start = client.post(f"/api/play/start/{item['target_quiz_id']}")
    assert start.status_code == 201, start.text
    token = start.json()["session_token"]
    ans = client.post(f"/api/play/{token}/answer", json={"answer": "javob4311"})
    assert ans.status_code == 200, ans.text
    assert ans.json()["is_correct"] is True

    target_db.expire_all()
    record = target_db.query(AnswerRecord).order_by(AnswerRecord.id.desc()).first()
    assert record.question_id == first_q["question_id"]
    assert record.round_question_id == first_q["round_question_id"]


@pytest.mark.parametrize("tgt, revision, approved, message", [
    ({"project_id": "old-bird-43200046", "branch_id": "br-small-base-b1pev96p", "endpoint_id": "ep-ancient-hall-b1lq9aa7"},
     "b2c3d4e5f6a7", None, "PRODUCTION branch"),
    ({"project_id": "old-bird-43200046", "branch_id": "br-green-moon-b19rc6de", "endpoint_id": "ep-damp-frog-b1y9sc7x"},
     "b2c3d4e5f6a7", None, "source database"),
    ({"project_id": "other-project", "branch_id": "br-x", "endpoint_id": "ep-x"}, "b2c3d4e5f6a7", None, "Neon project"),
    ({"project_id": "old-bird-43200046", "branch_id": "br-temp", "endpoint_id": "ep-temp"}, "9c0d1e2f3a4b", None, "Alembic"),
    ({"project_id": "old-bird-43200046", "branch_id": "br-small-base-b1pev96p", "endpoint_id": "ep-ancient-hall-b1lq9aa7"},
     "b2c3d4e5f6a7", "yes", "PRODUCTION branch"),
])
def test_identity_guards_refuse_unsafe_targets(tgt, revision, approved, message):
    src = {"project_id": "old-bird-43200046", "branch_id": "br-green-moon-b19rc6de", "endpoint_id": SRC}
    with pytest.raises(tp.TransferError, match=message):
        tp.check_identities(src, tgt, revision, approved)


def test_identity_guards_allow_temporary_branch_and_explicitly_approved_production():
    src = {"project_id": "old-bird-43200046", "branch_id": "br-green-moon-b19rc6de", "endpoint_id": SRC}
    tp.check_identities(src, {"project_id": "old-bird-43200046", "branch_id": "br-temp", "endpoint_id": "ep-temp"},
                        "b2c3d4e5f6a7", None)
    tp.check_identities(src, {"project_id": "old-bird-43200046", "branch_id": "br-small-base-b1pev96p",
                              "endpoint_id": "ep-ancient-hall-b1lq9aa7"}, "b2c3d4e5f6a7", "br-small-base-b1pev96p")
    with pytest.raises(tp.TransferError, match="Source must be"):
        tp.check_identities({**src, "endpoint_id": "ep-other"}, {"project_id": "old-bird-43200046",
                            "branch_id": "br-temp", "endpoint_id": "ep-temp"}, "b2c3d4e5f6a7", None)


def test_target_transaction_uses_few_round_trips(source_db, target_db):
    """Regression: v1 issued thousands of per-row statements, so the production transaction ran
    10+ minutes and its connection was dropped. v2 must stay at a small constant number."""
    from sqlalchemy import event

    source_packs = tp.load_source(source_db, list(tp.EXPECTED_PACKS))
    statements = []
    engine = target_db.get_bind()

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        report = tp.apply_transfer(source_packs, target_db, SRC)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert report["ok"], report["validation_errors"]
    # SQLite has no multi-row RETURNING batching here, so inserts may be per row; count only
    # non-INSERT statements (reads, validation, updates), which v1 scaled per question.
    non_insert = [s for s in statements if not s.lstrip().upper().startswith("INSERT")]
    assert len(non_insert) <= 40, f"{len(non_insert)} non-insert statements on target"


def test_direct_url_bypasses_neon_pooler():
    url = "postgresql://u:p@ep-ancient-hall-b1lq9aa7-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    assert tp.direct_url(url) == "postgresql://u:p@ep-ancient-hall-b1lq9aa7.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    assert tp.direct_url("postgresql://u:p@localhost/db") == "postgresql://u:p@localhost/db"
