"""
Attempt / leaderboard security fixes (audit follow-up).

1. Race: concurrent /start requests can never create two ranked attempts (DB-level per-user lock).
   Verified with REAL concurrent threads against a real SQLite FILE database (not the in-memory test DB).
2. Timer-mode mismatch: a resumed attempt reports ITS real timer mode, never the requested one.
3. Abandoned attempts are never auto-finalized by /results.
4. Reveal pause: answers/selects are rejected until /continue (direct API attacks).
5. Staging fails closed; preview attempts consume the ranked opportunity.
6. Svoya Igra leaderboard ranks by points; Zakovat unchanged.
7. Transparency: ranked/practice exposed by the API; profile stats exclude practice (history kept).
"""
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.play as play_module
from api.auth import create_access_token
from database import Base, get_db
from main import app
from models import Quiz, QuizVersion, SoloAttempt, AnswerRecord
from tests.test_ranked_attempt_rule import _add_version, _make_user, _headers, _attempt, _board
from tests.test_svoyak_gameplay import create_svoyak_test_game


@pytest.fixture
def pack(db_session):
    quiz = Quiz(title="Security Pack", description="2 savol")
    db_session.add(quiz)
    db_session.flush()
    _add_version(db_session, quiz, 1)
    return quiz


def _start(client, quiz, headers, **body):
    return client.post(f"/api/play/start/{quiz.id}", json=body, headers=headers)


# ---------------------------------------------------------------------------
# 1. Race condition — real threads, real database file
# ---------------------------------------------------------------------------

@pytest.fixture
def file_db(tmp_path):
    """Isolated SQLite FILE database with a real connection pool, so requests run on separate
    connections and genuinely contend for database locks."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    SessionFile = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        session = SessionFile()
        try:
            yield session
        finally:
            session.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    yield SessionFile
    if previous is not None:
        app.dependency_overrides[get_db] = previous
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _seed_file_db(SessionFile):
    s = SessionFile()
    try:
        quiz = Quiz(title="Race Pack", description="race")
        s.add(quiz)
        s.flush()
        _add_version(s, quiz, 1)
        user = _make_user(s, "race_real@zakowhat.uz", "Racer")
        return quiz.id, user.id, user.email
    finally:
        s.close()


def _fire_concurrent_starts(quiz_id, user_id, email, n=2):
    """Releases n /start requests at the same instant from separate threads and clients."""
    headers = {"Authorization": f"Bearer {create_access_token(user_id, email)}"}
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        client = TestClient(app)
        barrier.wait()
        res = client.post(f"/api/play/start/{quiz_id}", json={"timer_mode": "with_timer"}, headers=headers)
        results[i] = (res.status_code, res.json())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def _widen_race_window(monkeypatch):
    """Sleeps between the existence checks and the INSERT so overlapping requests are guaranteed
    to interleave if nothing serializes them."""
    original = play_module.pack_version_ids

    def slow_pack_version_ids(db, quiz_id):
        time.sleep(0.5)
        return original(db, quiz_id)

    monkeypatch.setattr(play_module, "pack_version_ids", slow_pack_version_ids)


def test_race_harness_reproduces_double_ranked_without_lock(file_db, monkeypatch):
    """Control experiment: with the DB lock disabled, the same concurrent requests DO create two ranked
    attempts — proving the harness really exercises the race (so the next test's pass is meaningful)."""
    quiz_id, user_id, email = _seed_file_db(file_db)
    _widen_race_window(monkeypatch)
    monkeypatch.setattr(play_module, "lock_user_for_attempt_creation", lambda db, uid: None)

    results = _fire_concurrent_starts(quiz_id, user_id, email)
    assert all(code == 201 for code, _ in results)

    s = file_db()
    try:
        ranked = [a for a in s.query(SoloAttempt).filter(SoloAttempt.user_id == user_id).all()
                  if (a.attempt_metadata or {}).get("competitive") is True]
        assert len(ranked) == 2  # the vulnerability, reproduced
    finally:
        s.close()


def test_concurrent_starts_create_exactly_one_ranked_attempt(file_db, monkeypatch):
    """With the per-user DB lock, simultaneous starts are serialized: the second request waits for the
    first to commit, then resumes that attempt. Exactly one attempt (ranked) exists."""
    quiz_id, user_id, email = _seed_file_db(file_db)
    _widen_race_window(monkeypatch)

    results = _fire_concurrent_starts(quiz_id, user_id, email)
    assert all(code == 201 for code, _ in results)
    tokens = {body["session_token"] for _, body in results}
    assert len(tokens) == 1
    assert sorted(bool(body.get("resumed")) for _, body in results) == [False, True]
    assert all(body["ranked"] is True for _, body in results)

    s = file_db()
    try:
        attempts = s.query(SoloAttempt).filter(SoloAttempt.user_id == user_id).all()
        assert len(attempts) == 1
        assert attempts[0].attempt_metadata["competitive"] is True
    finally:
        s.close()


def test_concurrent_restart_true_starts_never_add_a_ranked_attempt(file_db, monkeypatch):
    """Direct API attack: a burst of restart=true starts after the ranked attempt exists only creates
    practice attempts and leaves at most the original ranked attempt."""
    quiz_id, user_id, email = _seed_file_db(file_db)
    headers = {"Authorization": f"Bearer {create_access_token(user_id, email)}"}
    first = TestClient(app).post(f"/api/play/start/{quiz_id}", json={"timer_mode": "with_timer"}, headers=headers)
    assert first.json()["ranked"] is True
    _widen_race_window(monkeypatch)

    barrier = threading.Barrier(3)
    codes = []

    def worker():
        c = TestClient(app)
        barrier.wait()
        codes.append(c.post(f"/api/play/start/{quiz_id}", json={"timer_mode": "with_timer", "restart": True},
                            headers=headers).status_code)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert codes == [201, 201, 201]

    s = file_db()
    try:
        ranked = [a for a in s.query(SoloAttempt).filter(SoloAttempt.user_id == user_id).all()
                  if (a.attempt_metadata or {}).get("competitive") is True]
        assert [a.session_token for a in ranked] == [first.json()["session_token"]]
        assert ranked[0].status == "abandoned"  # ended by the new attempts; can never be finished later
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 2. Timer-mode mismatch on resume
# ---------------------------------------------------------------------------

def test_resume_reports_real_timer_mode_not_requested(client, db_session, pack):
    user = _make_user(db_session, "mismatch@zakowhat.uz")
    h = _headers(user)
    ranked = _start(client, pack, h, timer_mode="with_timer").json()

    # New tab / device asks for no-timer while the timed ranked attempt is active -> same attempt, REAL mode
    resumed = _start(client, pack, h, timer_mode="no_timer")
    assert resumed.status_code == status.HTTP_201_CREATED
    data = resumed.json()
    assert data["resumed"] is True
    assert data["session_token"] == ranked["session_token"]
    assert data["timer_mode"] == "with_timer"
    assert data["ranked"] is True
    assert data["current_state"]["question"]["time_limit_seconds"] > 0


def test_resume_of_no_timer_practice_reports_no_timer(client, db_session, pack):
    user = _make_user(db_session, "mismatch2@zakowhat.uz")
    h = _headers(user)
    _start(client, pack, h, timer_mode="with_timer")
    practice = _start(client, pack, h, timer_mode="no_timer", restart=True).json()
    assert practice["timer_mode"] == "no_timer" and practice["ranked"] is False

    resumed = _start(client, pack, h, timer_mode="with_timer").json()
    assert resumed["session_token"] == practice["session_token"]
    assert resumed["timer_mode"] == "no_timer"
    assert resumed["current_state"]["question"]["time_limit_seconds"] == 0


def test_frontend_takes_timer_mode_from_server():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "state.timerMode = data.timer_mode || timerMode;" in html
    assert "sessionStorage.setItem('zakowhat_timer_mode', state.timerMode);" in html
    assert "sessionStorage.setItem('zakowhat_timer_mode', timerMode);" not in html  # requested value never stored
    assert "state.timerMode = (question.time_limit_seconds === 0) ? 'no_timer' : 'with_timer';" in html
    assert "const timeLimit = (question.time_limit_seconds === 0) ? 0 : rawLimit;" in html


# ---------------------------------------------------------------------------
# 3. Abandoned attempts are never finalized by /results
# ---------------------------------------------------------------------------

def test_results_endpoint_rejects_abandoned_attempt(client, db_session, pack):
    user = _make_user(db_session, "abandoned@zakowhat.uz", "Abandoner")
    h = _headers(user)
    token = _start(client, pack, h, timer_mode="with_timer").json()["session_token"]
    client.post(f"/api/play/{token}/answer", json={"answer": "A1"}, headers=h)
    client.post(f"/api/play/{token}/answer", json={"answer": "A2"}, headers=h)  # all answered -> round_reveal
    assert _attempt(db_session, token).status == "round_reveal"

    client.post(f"/api/play/{token}/restart", headers=h)
    assert _attempt(db_session, token).status == "abandoned"

    res = client.get(f"/api/play/{token}/results", headers=h)
    assert res.status_code == status.HTTP_410_GONE
    assert _attempt(db_session, token).status == "abandoned"  # unchanged, not completed
    assert _board(client, pack) == []


# ---------------------------------------------------------------------------
# 4. Reveal pause: direct API attacks
# ---------------------------------------------------------------------------

def test_svoyak_classic_select_rejected_during_reveal_pause(client, db_session):
    user = _make_user(db_session, "pause_select@zakowhat.uz")
    h = _headers(user)
    quiz = create_svoyak_test_game(client, db_session, "Pause Select Game")
    token = _start(client, quiz, h, svoyak_mode="classic", timer_mode="with_timer").json()["session_token"]
    ans = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1_10"}, headers=h).json()
    assert ans["awaiting_continue"] is True

    sel = client.post(f"/api/play/{token}/select", json={"round_index": 0, "question_index": 1}, headers=h)
    assert sel.status_code == status.HTTP_409_CONFLICT
    blind = client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1_20"}, headers=h)
    assert blind.status_code == status.HTTP_409_CONFLICT
    assert client.post(f"/api/play/{token}/continue", headers=h).status_code == status.HTTP_200_OK
    assert client.post(f"/api/play/{token}/answer", json={"answer": "Javob 1_20"}, headers=h).json()["total_score"] == 30


# ---------------------------------------------------------------------------
# 5. Staging fails closed; previews consume the ranked opportunity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("environment", ["test", "development", "staging", "production"])
def test_staging_denied_without_allowlist_in_every_environment(client, db_session, monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-for-staging-gate-0123456789")  # required in production mode
    user = _make_user(db_session, f"stg_{environment}@zakowhat.uz")
    h = _headers(user)
    assert client.get("/api/play/staging/quizzes", headers=h).status_code == status.HTTP_403_FORBIDDEN
    assert client.post("/api/play/staging/start/1", headers=h).status_code == status.HTTP_403_FORBIDDEN
    monkeypatch.setenv("ADMIN_EMAILS", user.email)
    assert client.get("/api/play/staging/quizzes", headers=h).status_code == status.HTTP_200_OK


def test_preview_attempt_consumes_ranked_opportunity(client, db_session, pack):
    """Staff who previewed a pack (and so saw its answers) can only play it as practice after publication."""
    staff = _make_user(db_session, "staff_preview@zakowhat.uz")
    version = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == pack.id).first()
    db_session.add(SoloAttempt(user_id=staff.id, quiz_version_id=version.id, timer_mode="preview",
                               status="completed", attempt_metadata={"is_staging": True}))
    db_session.commit()

    h = _headers(staff)
    res = _start(client, pack, h, timer_mode="with_timer").json()
    assert res["ranked"] is False
    assert _attempt(db_session, res["session_token"]).attempt_metadata["competitive"] is False


# ---------------------------------------------------------------------------
# 6. Svoya Igra leaderboard by points; Zakovat unchanged
# ---------------------------------------------------------------------------

def _completed(db_session, user, version, correct, score, seconds=60, meta=None):
    a = SoloAttempt(user_id=user.id, quiz_version_id=version.id, timer_mode="standard", status="completed",
                    total_correct=correct, total_score=score, active_time_seconds=seconds,
                    completed_at=datetime.now(timezone.utc), attempt_metadata=meta or {})
    db_session.add(a)
    db_session.commit()
    return a


def test_svoyak_leaderboard_ranks_by_points(client, db_session):
    quiz = create_svoyak_test_game(client, db_session, "Points Ranking Game")
    version = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == quiz.id).first()
    guesser = _make_user(db_session, "guesser@zakowhat.uz", "Guesser")      # many correct, heavy penalties
    careful = _make_user(db_session, "careful@zakowhat.uz", "Careful")      # fewer correct, more points
    _completed(db_session, guesser, version, correct=20, score=-500)
    _completed(db_session, careful, version, correct=19, score=700)

    lb = client.get(f"/api/quizzes/{quiz.id}/leaderboard").json()
    assert lb["ranking_metric"] == "score"
    assert [r["display_name"] for r in lb["leaderboard"]] == ["Careful", "Guesser"]
    assert lb["leaderboard"][0]["total_score"] == 700


def test_svoyak_points_tie_broken_by_time(client, db_session):
    quiz = create_svoyak_test_game(client, db_session, "Points Tie Game")
    version = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == quiz.id).first()
    slow = _make_user(db_session, "slow@zakowhat.uz", "Slow")
    fast = _make_user(db_session, "fast@zakowhat.uz", "Fast")
    _completed(db_session, slow, version, correct=10, score=300, seconds=500)
    _completed(db_session, fast, version, correct=8, score=300, seconds=200)
    names = [r["display_name"] for r in client.get(f"/api/quizzes/{quiz.id}/leaderboard").json()["leaderboard"]]
    assert names == ["Fast", "Slow"]


def test_zakovat_leaderboard_still_ranks_by_correct_answers(client, db_session, pack):
    version = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == pack.id).first()
    more_correct = _make_user(db_session, "zk_more@zakowhat.uz", "More Correct")
    more_points = _make_user(db_session, "zk_points@zakowhat.uz", "More Points")
    _completed(db_session, more_correct, version, correct=2, score=2)
    _completed(db_session, more_points, version, correct=1, score=99)
    lb = client.get(f"/api/quizzes/{pack.id}/leaderboard").json()
    assert lb["ranking_metric"] == "correct"
    assert [r["display_name"] for r in lb["leaderboard"]] == ["More Correct", "More Points"]


# ---------------------------------------------------------------------------
# 7. Transparency + profile statistics
# ---------------------------------------------------------------------------

def test_api_exposes_ranked_status_on_start_restart_and_state(client, db_session, pack):
    user = _make_user(db_session, "transparent@zakowhat.uz")
    h = _headers(user)
    start = _start(client, pack, h, timer_mode="with_timer").json()
    assert start["ranked"] is True and start["timer_mode"] == "with_timer"
    state = client.get(f"/api/play/{start['session_token']}", headers=h).json()
    assert state["ranked"] is True and state["timer_mode"] == "with_timer"

    restarted = client.post(f"/api/play/{start['session_token']}/restart", headers=h).json()
    assert restarted["ranked"] is False
    state2 = client.get(f"/api/play/{restarted['session_token']}", headers=h).json()
    assert state2["ranked"] is False


def test_profile_stats_exclude_practice_but_keep_history(client, db_session, pack):
    user = _make_user(db_session, "profile_stats@zakowhat.uz", "Profiler")
    version = db_session.query(QuizVersion).filter(QuizVersion.quiz_id == pack.id).first()
    legacy = _completed(db_session, user, version, correct=1, score=1)                     # historical: counted
    _completed(db_session, user, version, correct=2, score=2, meta={"competitive": False})  # practice: not counted

    h = _headers(user)
    prof = client.get("/api/auth/profile", headers=h).json()
    assert prof["stats"]["total_games"] == 1
    assert prof["stats"]["total_score"] == 1
    assert prof["stats"]["practice_games"] == 1
    ranked_flags = sorted(g["ranked"] for g in prof["recent_games"])
    assert ranked_flags == [False, True]          # practice run still listed in history

    pub = client.get(f"/api/auth/users/{user.id}/public").json()
    assert pub["stats"]["total_games"] == 1 and pub["stats"]["practice_games"] == 1
    assert db_session.query(SoloAttempt).filter(SoloAttempt.id == legacy.id).count() == 1  # nothing deleted


def test_frontend_communicates_ranked_and_practice_status():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert 'id="gameplay-ranked-badge"' in html
    assert "function renderRankedBadge()" in html
    assert 'id="restart-practice-note"' in html
    assert 'id="no-timer-practice-note"' in html
    assert 'id="profile-practice-note"' in html
    assert "function formatLeaderboardResult(item, isSvoyak)" in html
    assert "data.ranking_metric === 'score'" in html
