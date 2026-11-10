import os
import threading
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient

from database import Base, get_db
from models import DailyQuestion, DailySubmission, User
from tests.conftest import test_engine, TestingSessionLocal
from main import app
from services.daily_question import (
    TASHKENT_TZ,
    get_tashkent_now,
    get_tashkent_today,
    get_verified_pool,
    is_candidate_eligible,
    get_or_schedule_daily_question,
    evaluate_daily_answer,
    submit_daily_answer
)


@pytest.fixture
def auth_users(db_session):
    """Creates two registered test users for Daily Question testing."""
    from api.auth import hash_password
    user1 = User(
        email="daily_user1@zakowhat.uz",
        display_name="Daily Player 1",
        hashed_password=hash_password("Pass123!"),
        auth_provider="local"
    )
    user2 = User(
        email="daily_user2@zakowhat.uz",
        display_name="Daily Player 2",
        hashed_password=hash_password("Pass123!"),
        auth_provider="local"
    )
    db_session.add_all([user1, user2])
    db_session.commit()
    db_session.refresh(user1)
    db_session.refresh(user2)
    return user1, user2


def test_1_same_date_returns_same_question_for_every_user(client, db_session, auth_users):
    """Test 1: Same date returns the exact same question for every user (authenticated and anonymous)."""
    user1, user2 = auth_users

    # Anonymous visitor (use client with cookies cleared)
    anon_client = TestClient(app)
    res_anon = anon_client.get("/api/daily/today")
    assert res_anon.status_code == 200
    data_anon = res_anon.json()
    assert data_anon["status"] == "available"
    assert data_anon["question"] is not None
    q_id = data_anon["question"]["id"]
    q_text = data_anon["question"]["text"]
    # Answer must NOT be revealed to user who hasn't submitted
    assert "correct_answer" not in data_anon["question"]

    # User 1
    from api.auth import create_access_token
    token1 = create_access_token(user1.id, user1.email)
    res_user1 = client.get("/api/daily/today", headers={"Authorization": f"Bearer {token1}"})
    assert res_user1.status_code == 200
    data_user1 = res_user1.json()
    assert data_user1["question"]["id"] == q_id
    assert data_user1["question"]["text"] == q_text
    assert data_user1["has_submitted"] is False

    # User 2
    token2 = create_access_token(user2.id, user2.email)
    res_user2 = client.get("/api/daily/today", headers={"Authorization": f"Bearer {token2}"})
    assert res_user2.status_code == 200
    data_user2 = res_user2.json()
    assert data_user2["question"]["id"] == q_id
    assert data_user2["question"]["text"] == q_text
    assert data_user2["has_submitted"] is False

    # Verify exactly one DailyQuestion row in DB
    all_dq = db_session.query(DailyQuestion).all()
    assert len(all_dq) == 1


def test_2_date_boundaries_follow_asia_tashkent(monkeypatch):
    """Test 2: Date boundaries follow Asia/Tashkent (UTC+5), not server UTC date."""
    # Scenario A: 2026-10-28 at 20:00:00 UTC
    # In UTC: 2026-10-28
    # In Asia/Tashkent: 2026-10-29 01:00:00 (Next day!)
    utc_time_night = datetime(2026, 10, 28, 20, 0, 0, tzinfo=timezone.utc)

    class MockDatetimeA:
        @classmethod
        def now(cls, tz=None):
            return utc_time_night.astimezone(tz)

    import services.daily_question as dq_service
    monkeypatch.setattr(dq_service, "datetime", MockDatetimeA)

    tashkent_d = dq_service.get_tashkent_today()
    assert tashkent_d == date(2026, 10, 29)
    assert tashkent_d != utc_time_night.date()

    # Scenario B: 2026-10-28 at 18:59:59 UTC
    # In Asia/Tashkent: 2026-10-28 23:59:59 (Same day)
    utc_time_before_midnight = datetime(2026, 10, 28, 18, 59, 59, tzinfo=timezone.utc)

    class MockDatetimeB:
        @classmethod
        def now(cls, tz=None):
            return utc_time_before_midnight.astimezone(tz)

    monkeypatch.setattr(dq_service, "datetime", MockDatetimeB)
    assert dq_service.get_tashkent_today() == date(2026, 10, 28)


def test_3_scheduled_question_stable_after_restart(db_session):
    """Test 3: A scheduled question remains stable across application/session restarts."""
    test_date = date(2026, 11, 1)

    # First scheduling
    dq1 = get_or_schedule_daily_question(db_session, target_date=test_date)
    assert dq1 is not None
    first_id = dq1.id
    first_msg_id = dq1.message_id
    first_text = dq1.question_text

    # Commit and close session to simulate complete restart
    db_session.commit()
    db_session.close()

    # Re-open session and query again
    fresh_session = TestingSessionLocal()
    try:
        dq2 = get_or_schedule_daily_question(fresh_session, target_date=test_date)
        assert dq2 is not None
        assert dq2.id == first_id
        assert dq2.message_id == first_msg_id
        assert dq2.question_text == first_text
    finally:
        fresh_session.close()


def test_4_concurrent_requests_cannot_schedule_different_questions():
    """Test 4: Concurrent requests cannot schedule different questions or create duplicate rows."""
    from sqlalchemy.orm import sessionmaker
    SessionMaker = sessionmaker(bind=test_engine)
    test_date = date(2026, 11, 5)

    results = []
    errors = []

    def worker():
        session = SessionMaker()
        try:
            dq = get_or_schedule_daily_question(session, target_date=test_date)
            results.append(dq.id)
        except Exception as e:
            errors.append(e)
        finally:
            session.close()

    # Launch 8 concurrent threads trying to schedule the same date simultaneously
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(results) == 8
    # All 8 workers must receive the exact same DailyQuestion ID
    assert len(set(results)) == 1

    # Verify database has exactly 1 record for this date
    verify_session = SessionMaker()
    count = verify_session.query(DailyQuestion).filter(DailyQuestion.schedule_date == test_date).count()
    verify_session.close()
    assert count == 1


def test_5_question_not_repeated_before_pool_exhausted(db_session):
    """Test 5: Questions are not repeated before the eligible pool is exhausted."""
    # Test with custom 5-item pool
    sample_pool = [
        {"question_id": f"q_{i}", "message_id": 100 + i, "clean_text": f"Question {i}?", "primary_answer": f"Ans {i}", "accepted_answers": [f"Ans {i}"]}
        for i in range(1, 6)
    ]

    base_date = date(2026, 12, 1)
    scheduled_msg_ids = []

    for day_offset in range(5):
        cur_date = base_date + timedelta(days=day_offset)
        dq = get_or_schedule_daily_question(db_session, target_date=cur_date, pool=sample_pool)
        assert dq is not None
        assert dq.message_id not in scheduled_msg_ids
        scheduled_msg_ids.append(dq.message_id)

    # 5 unique questions scheduled for 5 consecutive days
    assert len(set(scheduled_msg_ids)) == 5


def test_6_exhausted_pool_produces_explicit_unavailable_state(client, db_session, monkeypatch):
    """Test 6: An exhausted pool produces an explicit unavailable state, not a repeat or crash."""
    small_pool = [
        {"question_id": "q_single", "message_id": 999, "clean_text": "Single Q?", "primary_answer": "A", "accepted_answers": ["A"]}
    ]

    # Day 1: Schedules the single question
    d1 = date(2026, 6, 1)
    dq1 = get_or_schedule_daily_question(db_session, target_date=d1, pool=small_pool)
    assert dq1 is not None

    # Day 2: Pool is now exhausted
    d2 = date(2026, 6, 2)
    dq2 = get_or_schedule_daily_question(db_session, target_date=d2, pool=small_pool)
    assert dq2 is None

    # Test API endpoint response when pool is exhausted
    import api.daily as daily_api
    monkeypatch.setattr(daily_api, "get_or_schedule_daily_question", lambda db, target_date=None: None)

    res = client.get("/api/daily/today")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "unavailable"
    assert data["question"] is None
    assert "Bugun uchun savol mavjud emas" in data["message"]


def test_7_ineligible_records_never_scheduled(db_session):
    """Test 7: Ineligible records (multi-part, emoji triplets, missing media) are skipped."""
    dirty_pool = [
        # Ineligible: Multi-part emoji triplet
        {
            "question_id": "q_emoji_triplet",
            "message_id": 501,
            "clean_text": "1⃣ Birinchi savol? 2⃣ Ikkinchi savol?",
            "primary_answer": "1. A, 2. B"
        },
        # Ineligible: Missing media reference in text
        {
            "question_id": "q_missing_photo",
            "message_id": 502,
            "clean_text": "Ushbu suratdagi qushni toping?",
            "primary_answer": "Burgut",
            "source_meta": {"has_media": True, "media_url": None}
        },
        # Ineligible: Blocked status
        {
            "question_id": "q_blocked",
            "message_id": 503,
            "clean_text": "Mangled question?",
            "primary_answer": "None",
            "daily_question_status": "blocked"
        },
        # ELIGIBLE candidate
        {
            "question_id": "q_valid_clean",
            "message_id": 504,
            "clean_text": "Qadimgi Yunoniston poytaxti qaysi shahar?",
            "primary_answer": "Afina",
            "accepted_answers": ["Afina", "Athens"]
        }
    ]

    # Verify eligibility filter
    assert is_candidate_eligible(dirty_pool[0]) is False
    assert is_candidate_eligible(dirty_pool[1]) is False
    assert is_candidate_eligible(dirty_pool[2]) is False
    assert is_candidate_eligible(dirty_pool[3]) is True

    # Scheduler should skip the 3 invalid ones and pick the 4th
    scheduled = get_or_schedule_daily_question(db_session, target_date=date(2026, 7, 1), pool=dirty_pool)
    assert scheduled is not None
    assert scheduled.message_id == 504
    assert scheduled.question_text == "Qadimgi Yunoniston poytaxti qaysi shahar?"


def test_8_source_question_and_answer_text_remain_unchanged(db_session):
    """Test 8: Source question and answer text remain 100% exact and unchanged in database."""
    verified_pool = get_verified_pool()
    assert len(verified_pool) >= 365
    assert len(verified_pool) == 1000

    first_cand = verified_pool[0]
    scheduled = get_or_schedule_daily_question(db_session, target_date=date(2026, 8, 1), pool=verified_pool)

    # Exact character-for-character equality
    assert scheduled.question_text == first_cand["clean_text"]
    assert scheduled.primary_answer == first_cand["primary_answer"]
    assert scheduled.question_id == first_cand["question_id"]
    assert scheduled.message_id == first_cand["message_id"]


def test_9_user_submissions_stored_independently(client, db_session, auth_users):
    """Test 9: User submissions are stored independently without mutating the source DailyQuestion."""
    user1, user2 = auth_users
    from api.auth import create_access_token

    token1 = create_access_token(user1.id, user1.email)
    token2 = create_access_token(user2.id, user2.email)

    # Question for today is scheduled (Msg 2: Papua Yangi Gvineyasi)
    # User 1 submits correct answer
    res1 = client.post(
        "/api/daily/submit",
        json={"answer": "Papua Yangi Gvineya"},
        headers={"Authorization": f"Bearer {token1}"}
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "success"
    assert data1["is_correct"] is True
    assert data1["correct_answer"] == "Papua Yangi Gvineyasi"

    # User 2 submits incorrect answer (Try 1 of 3)
    res2 = client.post(
        "/api/daily/submit",
        json={"answer": "Madagaskar"},
        headers={"Authorization": f"Bearer {token2}"}
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "incorrect"
    assert data2["is_correct"] is False
    assert data2["attempts_used"] == 1
    assert data2["attempts_remaining"] == 2
    assert data2["is_finished"] is False
    # Correct answer MUST NOT be revealed when tries remain!
    assert "correct_answer" not in data2

    # User 2 submits second incorrect answer (Try 2 of 3)
    res2_try2 = client.post(
        "/api/daily/submit",
        json={"answer": "Avstraliya"},
        headers={"Authorization": f"Bearer {token2}"}
    )
    assert res2_try2.status_code == 200
    data2_try2 = res2_try2.json()
    assert data2_try2["status"] == "incorrect"
    assert data2_try2["attempts_used"] == 2
    assert data2_try2["attempts_remaining"] == 1
    assert "correct_answer" not in data2_try2

    # User 2 submits third incorrect answer (Try 3 of 3 - Exhausted!)
    res2_try3 = client.post(
        "/api/daily/submit",
        json={"answer": "Fiji"},
        headers={"Authorization": f"Bearer {token2}"}
    )
    assert res2_try3.status_code == 200
    data2_try3 = res2_try3.json()
    assert data2_try3["status"] == "finished"
    assert data2_try3["attempts_used"] == 3
    assert data2_try3["attempts_remaining"] == 0
    assert data2_try3["is_finished"] is True
    # Now correct answer is revealed
    assert data2_try3["correct_answer"] == "Papua Yangi Gvineyasi"

    # User 2 tries a 4th time -> 409 Conflict
    res2_try4 = client.post(
        "/api/daily/submit",
        json={"answer": "Yana bitta"},
        headers={"Authorization": f"Bearer {token2}"}
    )
    assert res2_try4.status_code == 409

    # Check database records
    subs = db_session.query(DailySubmission).order_by(DailySubmission.id).all()
    assert len(subs) == 2

    assert subs[0].user_id == user1.id
    assert subs[0].is_correct is True
    assert subs[0].submitted_text == "Papua Yangi Gvineya"

    assert subs[1].user_id == user2.id
    assert subs[1].is_correct is False
    assert subs[1].submitted_text == "Fiji"
    assert subs[1].submission_meta["attempts"] == 3
    assert subs[1].submission_meta["history"] == ["Madagaskar", "Avstraliya", "Fiji"]

    # Verify source DailyQuestion is completely unchanged
    dq = db_session.query(DailyQuestion).first()
    assert dq.primary_answer == "Papua Yangi Gvineyasi"
    assert dq.question_text == "Bu davlatni <<Etnograf uchun jannat, har qanday hukumat uchun haqiqiy jahannam>> deb atashadi. Mamlakatning bosh shiori esa-<<Yu-mi van-pela pipal>>dir. Savol: Gap qaysi mamlakat haqida ketmoqda?"

    # Duplicate submission attempt: User 1 tries to submit again -> 409 Conflict
    res_dup = client.post(
        "/api/daily/submit",
        json={"answer": "Boshqa javob"},
        headers={"Authorization": f"Bearer {token1}"}
    )
    assert res_dup.status_code == 409

    # Unauthenticated submission attempt -> 401 Unauthorized
    anon_client = TestClient(app)
    res_unauth = anon_client.post(
        "/api/daily/submit",
        json={"answer": "Test"}
    )
    assert res_unauth.status_code == 401


def test_10_existing_classic_gameplay_and_auth_continue_to_work(client, db_session, seed_sample_quiz):
    """Test 10: Existing Classic Zakovat gameplay, quizzes, and auth continue to function seamlessly."""
    # 1. Start a Classic gameplay attempt
    res_start = client.post(f"/api/play/start/{seed_sample_quiz.id}")
    assert res_start.status_code == 201
    start_data = res_start.json()
    token = start_data["session_token"]
    assert token is not None

    # 2. Answer question 1 in the classic quiz (Standard)
    res_ans = client.post(f"/api/play/{token}/answer", json={"answer": "toshkent"})
    assert res_ans.status_code == 200
    assert res_ans.json()["is_correct"] is True

    # 3. Concurrently use Daily Question with the same client
    res_daily = client.get("/api/daily/today")
    assert res_daily.status_code == 200
    daily_data = res_daily.json()
    assert daily_data["status"] == "available"

    res_daily_sub = client.post(
        "/api/daily/submit",
        json={"answer": "Papua Yangi Gvineyasi"}
    )
    assert res_daily_sub.status_code == 200
    assert res_daily_sub.json()["is_correct"] is True

    # 4. Verify classic attempt state is completely unaffected
    res_state = client.get(f"/api/play/{token}")
    assert res_state.status_code == 200
    assert res_state.json()["total_score"] >= 1


def test_11_dataset_loading_and_schema_integrity():
    """Test 11: Production Daily Question dataset loads cleanly and satisfies strict schema integrity."""
    pool = get_verified_pool()
    assert len(pool) >= 365
    assert len(pool) == 1000

    required_keys = {"question_id", "message_id", "channel_id", "clean_text", "primary_answer", "accepted_answers", "source_meta"}
    for idx, item in enumerate(pool):
        assert isinstance(item, dict), f"Item #{idx} is not a dictionary"
        missing = required_keys - set(item.keys())
        assert not missing, f"Item #{idx} missing keys: {missing}"

        assert isinstance(item["question_id"], str) and len(item["question_id"]) > 0
        assert isinstance(item["message_id"], int) and item["message_id"] > 0
        assert isinstance(item["channel_id"], int)
        assert isinstance(item["clean_text"], str) and len(item["clean_text"].strip()) >= 20
        assert isinstance(item["primary_answer"], str) and len(item["primary_answer"].strip()) >= 1
        assert isinstance(item["accepted_answers"], list) and len(item["accepted_answers"]) >= 1
        assert item["primary_answer"] in item["accepted_answers"]
        assert isinstance(item["source_meta"], dict)

        # Ensure every single question in the bank satisfies eligibility filter
        assert is_candidate_eligible(item) is True, f"Item #{idx} (MID {item['message_id']}) failed eligibility filter!"


def test_12_unique_ids_and_zero_duplicate_prevention():
    """Test 12: All question IDs, message IDs, and normalized question texts in bank are 100% unique."""
    import re
    pool = get_verified_pool()

    q_ids = [item["question_id"] for item in pool]
    m_ids = [item["message_id"] for item in pool]

    assert len(q_ids) == len(set(q_ids)), f"Duplicate question_ids found: {len(q_ids)} vs {len(set(q_ids))}"
    assert len(m_ids) == len(set(m_ids)), f"Duplicate message_ids found: {len(m_ids)} vs {len(set(m_ids))}"

    def norm(t: str) -> str:
        t = t.lower()
        t = re.sub(r'[\'ʻ`’"“”«».,!?:;\-–—\(\)\[\]{}]', '', t)
        return re.sub(r'\s+', ' ', t).strip()

    normalized_texts = [norm(item["clean_text"]) for item in pool]
    assert len(normalized_texts) == len(set(normalized_texts)), "Duplicate question texts found in production bank!"


def test_13_extended_scheduling_horizon_and_no_repeats(db_session):
    """Test 13: Scheduling transitions seamlessly across 40 days without repeats or collision."""
    pool = get_verified_pool()
    start_date = date(2027, 1, 1)

    scheduled_mids = []
    scheduled_qids = []

    for day_offset in range(40):
        target_d = start_date + timedelta(days=day_offset)
        dq = get_or_schedule_daily_question(db_session, target_date=target_d, pool=pool)
        assert dq is not None, f"Scheduling returned None on day offset {day_offset}"
        assert dq.message_id not in scheduled_mids, f"Repeat message_id on day offset {day_offset}: {dq.message_id}"
        assert dq.question_id not in scheduled_qids, f"Repeat question_id on day offset {day_offset}: {dq.question_id}"

        scheduled_mids.append(dq.message_id)
        scheduled_qids.append(dq.question_id)

    # First 33 days must match the original pilot questions
    for i in range(33):
        assert scheduled_mids[i] == pool[i]["message_id"]
        assert scheduled_qids[i] == pool[i]["question_id"]

    # Days 34 to 40 must transition to expanded questions seamlessly
    for i in range(33, 40):
        assert scheduled_mids[i] == pool[i]["message_id"]
        assert scheduled_qids[i] == pool[i]["question_id"]

    assert len(set(scheduled_mids)) == 40
    assert len(set(scheduled_qids)) == 40


def test_14_compatibility_with_existing_submissions_and_streaks(client, db_session, auth_users):
    """Test 14: Submissions and streaks work seamlessly on newly scheduled expanded bank days."""
    user1, _ = auth_users
    from api.auth import create_access_token

    token1 = create_access_token(user1.id, user1.email)
    pool = get_verified_pool()

    # Schedule 35 consecutive days to traverse into the expanded bank
    start_d = date(2027, 3, 1)
    dq = None
    for day_offset in range(35):
        d = start_d + timedelta(days=day_offset)
        dq = get_or_schedule_daily_question(db_session, target_date=d, pool=pool)

    # The 35th scheduled question is from the expanded bank (index 34)
    assert dq is not None
    assert dq.message_id == pool[34]["message_id"]
    assert dq.question_id == pool[34]["question_id"]

    # Player submits correct answer to this expanded question
    correct_ans = dq.primary_answer
    sub, already_existed = submit_daily_answer(
        db=db_session,
        daily_question=dq,
        user=user1,
        submitted_text=correct_ans,
        client_ip="127.0.0.1"
    )
    assert already_existed is False
    assert sub.is_correct is True
    assert sub.submitted_text == correct_ans
    assert sub.daily_question_id == dq.id

    # Duplicate submission to same day returns already_existed=True
    sub_dup, already_dup = submit_daily_answer(
        db=db_session,
        daily_question=dq,
        user=user1,
        submitted_text="Another",
        client_ip="127.0.0.1"
    )
    assert already_dup is True
    assert sub_dup.id == sub.id


def test_15_three_tries_flow_and_second_try_win(client, db_session, auth_users):
    """Test 15: User fails on Try 1, succeeds on Try 2, gets victory, and preserves streak."""
    user1, _ = auth_users
    from api.auth import create_access_token

    token = create_access_token(user1.id, user1.email)

    # 1. First visit today's question
    res_get1 = client.get("/api/daily/today", headers={"Authorization": f"Bearer {token}"})
    assert res_get1.status_code == 200
    assert res_get1.json()["has_submitted"] is False
    assert "correct_answer" not in res_get1.json()["question"]

    # 2. Try 1: Incorrect guess
    res_try1 = client.post(
        "/api/daily/submit",
        json={"answer": "noto'g'ri birinchi taxmin"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_try1.status_code == 200
    d1 = res_try1.json()
    assert d1["status"] == "incorrect"
    assert d1["is_correct"] is False
    assert d1["is_finished"] is False
    assert d1["attempts_used"] == 1
    assert d1["attempts_remaining"] == 2
    assert "correct_answer" not in d1

    # 3. Reload page during active attempt (re-check GET /api/daily/today)
    res_get2 = client.get("/api/daily/today", headers={"Authorization": f"Bearer {token}"})
    assert res_get2.status_code == 200
    d_get2 = res_get2.json()
    assert d_get2["has_submitted"] is False  # User can still submit!
    assert d_get2["user_submission"]["attempts_used"] == 1
    assert d_get2["user_submission"]["attempts_remaining"] == 2
    assert d_get2["user_submission"]["history"] == ["noto'g'ri birinchi taxmin"]
    assert "correct_answer" not in d_get2["question"]  # Answer still hidden!

    # 4. Try 2: Correct guess!
    res_try2 = client.post(
        "/api/daily/submit",
        json={"answer": "Papua Yangi Gvineyasi"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_try2.status_code == 200
    d2 = res_try2.json()
    assert d2["status"] == "success"
    assert d2["is_correct"] is True
    assert d2["is_finished"] is True
    assert d2["attempts_used"] == 2
    assert d2["attempts_remaining"] == 1
    assert d2["correct_answer"] == "Papua Yangi Gvineyasi"

    # 5. Reload page after victory
    res_get3 = client.get("/api/daily/today", headers={"Authorization": f"Bearer {token}"})
    assert res_get3.status_code == 200
    d_get3 = res_get3.json()
    assert d_get3["has_submitted"] is True
    assert d_get3["user_submission"]["is_correct"] is True
    assert d_get3["user_submission"]["attempts_used"] == 2
    assert d_get3["question"]["correct_answer"] == "Papua Yangi Gvineyasi"

    # 6. Any further attempt -> 409 Conflict
    res_try3 = client.post(
        "/api/daily/submit",
        json={"answer": "Yana urinish"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res_try3.status_code == 409



def test_daily_question_visual_dependency_suffix_regression():
    """
    Regression test for visual-dependency filter:
    1. Rejects questions referencing visual artifacts with suffix variations like 'rasmdagidan'.
    2. Does NOT create false positives for words with 'rasm' prefix like 'rasmiy'.
    3. Confirms daily_questions_bank has exactly 1000 items, replaced 335, and contains 360.
    """
    from services.daily_question import is_candidate_eligible

    # 1. Suffix variation 'rasmdagidan' without media_url is rejected
    q_suffix = {
        "question_id": "test_suffix_q",
        "clean_text": "Istanbuldagi masjidlarda deyarli har birida shunaqa chiroyli naqsh qilingan rasmdagidan ko'rish mumkin. Savol bu narsa nima maqsadda masjidlarga qurilgan?",
        "primary_answer": "Kabutarlar uchun inlar",
        "source_meta": {"has_media": False, "media_url": None}
    }
    assert is_candidate_eligible(q_suffix) is False

    # 2. Non-visual question containing 'rasmiy' is eligible
    q_rasmiy = {
        "question_id": "test_rasmiy_q",
        "clean_text": "O'zbekiston Respublikasining rasmiy ramzlari qachon qabul qilingan?",
        "primary_answer": "1991-yil",
        "accepted_answers": ["1991", "1991-yil"]
    }
    assert is_candidate_eligible(q_rasmiy) is True

    # 3. Bank integrity verification
    import json
    with open("data/daily/daily_questions_bank.json", "r", encoding="utf-8") as f:
        bank = json.load(f)

    assert len(bank) == 1000
    qids = [q["question_id"] for q in bank]
    assert len(qids) == len(set(qids)), "Duplicate IDs in bank!"
    assert "eski_2010763825_335" not in qids
    assert "eski_2010763825_360" in qids
    assert bank[192]["question_id"] == "eski_2010763825_360"
    assert bank[192]["primary_answer"] == "Iuda"


def test_repeated_answer_does_not_consume_an_attempt(client, db_session, auth_users):
    """Regression: typing an answer already tried (e.g. "k" twice) consumed an attempt but was not
    added to the history, so the player saw two answers and "3 ta urinishingiz ham tugadi"."""
    user1, _ = auth_users
    from api.auth import create_access_token
    headers = {"Authorization": f"Bearer {create_access_token(user1.id, user1.email)}"}

    def submit(answer):
        return client.post("/api/daily/submit", json={"answer": answer}, headers=headers)

    assert submit("idk").json()["attempts_used"] == 1
    assert submit("k").json()["attempts_used"] == 2

    # Repeats (exact, different case/spacing) are reported and cost nothing.
    for repeat in ("k", " K ", "IDK"):
        res = submit(repeat)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "duplicate"
        assert body["is_finished"] is False
        assert body["attempts_used"] == 2 and body["attempts_remaining"] == 1
        assert body["history"] == ["idk", "k"]
        assert "correct_answer" not in body
        assert "urinish sarflanmadi" in body["message"]

    today = client.get("/api/daily/today", headers=headers).json()
    assert today["has_submitted"] is False
    assert today["user_submission"]["attempts_used"] == 2

    # The real third attempt still works, is recorded, and finishes the question.
    third = submit("uchinchi boshqa javob").json()
    assert third["status"] == "finished"
    assert third["attempts_used"] == 3
    assert third["history"] == ["idk", "k", "uchinchi boshqa javob"]
    assert len(third["history"]) == third["attempts_used"]
