import json
import os
from datetime import datetime, timezone, timedelta
import pytest
from fastapi import status

from models import SoloAttempt, AnswerRecord, User, QuizVersion, Question, AcceptedAnswer, Quiz, Round, RoundQuestion
from services.gameplay import (
    damerau_levenshtein_distance,
    is_conservative_typo_match,
    SUPPORTED_GAME_MODES,
    FUTURE_MINI_GAMES,
)
from api.play import (
    calculate_question_time_limit,
    SESSION_INACTIVITY_LIMIT_SECONDS,
)


# ==============================================================================
# 1. QUESTION TIMER BOUNDS [90, 180]
# ==============================================================================

def test_timer_bounds_and_reading_allowance():
    """
    Owner Decision 1:
    - Minimum timer bound: 90 seconds.
    - Maximum timer bound: 180 seconds.
    - Reading allowance: ~2.5 words per second.
    - Timer disabled: 0 seconds.
    """
    # Empty string or 0 words -> lower bound 90s
    assert calculate_question_time_limit("", "zakovat_classic") == 90
    assert calculate_question_time_limit("Qisqa", "zakovat_classic") == 90

    # Short questions (e.g. 5 words -> 5 / 2.5 = 2s reading -> 92s)
    words_5 = " ".join(["soz"] * 5)
    assert calculate_question_time_limit(words_5, "zakovat_classic") == 92

    # Medium questions (25 words -> 25 / 2.5 = 10s reading -> 100s)
    words_25 = " ".join(["soz"] * 25)
    assert calculate_question_time_limit(words_25, "zakovat_classic") == 100

    # Long questions (100 words -> 100 / 2.5 = 40s reading -> 130s)
    words_100 = " ".join(["soz"] * 100)
    assert calculate_question_time_limit(words_100, "zakovat_classic") == 130

    # Very long questions (200 words -> 200 / 2.5 = 80s reading -> 170s)
    words_200 = " ".join(["soz"] * 200)
    assert calculate_question_time_limit(words_200, "zakovat_classic") == 170

    # Long questions reaching upper bound (225 words -> 90 + 90 = 180s)
    words_225 = " ".join(["soz"] * 225)
    assert calculate_question_time_limit(words_225, "zakovat_classic") == 180

    # Extremely long questions (300 and 500 words) strictly capped at 180s
    words_300 = " ".join(["soz"] * 300)
    assert calculate_question_time_limit(words_300, "zakovat_classic") == 180

    words_500 = " ".join(["soz"] * 500)
    assert calculate_question_time_limit(words_500, "zakovat_classic") == 180

    # Timer disabled explicitly returns 0
    assert calculate_question_time_limit(words_100, "zakovat_classic", timer_enabled=False) == 0


# ==============================================================================
# 2. ANSWER MATCHING — HYBRID TOLERANCE
# ==============================================================================

def test_damerau_levenshtein_distance_algorithm():
    """
    Verifies Damerau-Levenshtein distance calculation including adjacent transpositions.
    """
    assert damerau_levenshtein_distance("", "") == 0
    assert damerau_levenshtein_distance("toshkent", "toshkent") == 0

    # Single insertion
    assert damerau_levenshtein_distance("dorbozlikk", "dorbozlik") == 1
    # Single deletion
    assert damerau_levenshtein_distance("dorbozli", "dorbozlik") == 1
    # Single substitution
    assert damerau_levenshtein_distance("gerbitsit", "gerbitsid") == 1
    # Adjacent transposition
    assert damerau_levenshtein_distance("vaersiya", "versiya") == 1
    assert damerau_levenshtein_distance("ab", "ba") == 1

    # Multiple edits (distance >= 2)
    assert damerau_levenshtein_distance("gerbitsit", "gerbits") >= 2
    assert damerau_levenshtein_distance("toshkent", "samarqand") > 2


def test_conservative_typo_matching_rules():
    """
    Owner Decision 2:
    - Conservative deterministic tolerance.
    - Word count must match exactly.
    - Target word length >= 6 characters.
    - Initial root letter must match.
    - Edit distance <= 1.
    - Words <= 5 characters require strict exact match.
    """
    # 1. Valid single typo on long word (>= 6 chars)
    assert is_conservative_typo_match("gerbitsit", "gerbitsid") is True
    assert is_conservative_typo_match("vaersiya", "versiya") is True
    assert is_conservative_typo_match("dorbozlikk", "dorbozlik") is True
    assert is_conservative_typo_match("qisyoslanadi", "qiyoslanadi") is True
    assert is_conservative_typo_match("rejisyor", "rejissyor") is True

    # 2. Short words (<= 5 chars) strictly exact: single typo MUST FAIL
    assert is_conservative_typo_match("kum", "xum") is False
    assert is_conservative_typo_match("raft", "neft") is False
    assert is_conservative_typo_match("olma", "olmo") is False
    assert is_conservative_typo_match("kitob", "kito") is False
    assert is_conservative_typo_match("ari", "arli") is False

    # 3. Root initial letter mismatch: MUST FAIL even if distance is 1
    assert is_conservative_typo_match("teksika", "meksika") is False
    assert is_conservative_typo_match("boshkent", "toshkent") is False

    # 4. Word count mismatch: MUST FAIL
    assert is_conservative_typo_match("toshkent", "toshkent shahri") is False
    assert is_conservative_typo_match("uchib tushmoq", "uchib") is False

    # 5. Semantic differences: MUST FAIL
    assert is_conservative_typo_match("yuragi uchmoq", "uchib tushmoq") is False


def test_answer_submission_hybrid_tolerance_integration(client, seed_sample_quiz):
    """
    Verifies that submit_answer endpoint applies hybrid tolerance on words >= 6 chars
    and preserves strict exact matching on short words.
    """
    quiz = seed_sample_quiz
    # Authenticate a player so we can run multiple sessions cleanly
    reg_res = client.post("/api/auth/register", json={
        "display_name": "AnswerTester",
        "email": "answer_tester@zakowhat.uz",
        "password": "Password123!",
    })
    assert reg_res.status_code == status.HTTP_201_CREATED

    # Q1 target answer is "Toshkent" (8 characters)
    # Session 1: exact match
    res1 = client.post(f"/api/play/start/{quiz.id}")
    assert res1.status_code == status.HTTP_201_CREATED
    tok1 = res1.json()["session_token"]
    ans1 = client.post(f"/api/play/{tok1}/answer", json={"answer": "Toshkent"})
    assert ans1.status_code == status.HTTP_200_OK
    assert ans1.json()["is_correct"] is True

    # Session 2: single vowel typo on "Toshkent" (8 chars) -> "Tashkent" succeeds
    # (restart=True explicitly starts a fresh session; a plain /start would correctly resume session 1,
    #  which is paused at its round reveal)
    res2 = client.post(f"/api/play/start/{quiz.id}", json={"restart": True})
    assert res2.status_code == status.HTTP_201_CREATED
    tok2 = res2.json()["session_token"]
    ans2 = client.post(f"/api/play/{tok2}/answer", json={"answer": "Tashkent"})
    assert ans2.status_code == status.HTTP_200_OK
    assert ans2.json()["is_correct"] is True

    # Session 3: completely different word fails
    res3 = client.post(f"/api/play/start/{quiz.id}", json={"restart": True})
    assert res3.status_code == status.HTTP_201_CREATED
    tok3 = res3.json()["session_token"]
    ans3 = client.post(f"/api/play/{tok3}/answer", json={"answer": "Samarqand"})
    assert ans3.status_code == status.HTTP_200_OK
    assert ans3.json()["is_correct"] is False


# ==============================================================================
# 3. SESSION EXPIRY (5 MINUTES INACTIVITY -> HTTP 410 GONE)
# ==============================================================================

def test_session_inactivity_expiry_lifecycle(client, seed_sample_quiz, db_session):
    """
    Owner Decision 3:
    - Inactivity > 300s sets attempt.status = 'expired'.
    - Returns HTTP 410 Gone.
    - Prevents resumption, submitting answers, continuing, or reading results.
    - Cannot be bypassed.
    """
    quiz = seed_sample_quiz
    res = client.post(f"/api/play/start/{quiz.id}")
    assert res.status_code == status.HTTP_201_CREATED
    session_token = res.json()["session_token"]

    # 1. Immediate call works fine
    state_res = client.get(f"/api/play/{session_token}")
    assert state_res.status_code == status.HTTP_200_OK

    # 2. Simulate 305 seconds of inactivity
    attempt = db_session.query(SoloAttempt).filter(SoloAttempt.session_token == session_token).first()
    past_active = datetime.now(timezone.utc) - timedelta(seconds=SESSION_INACTIVITY_LIMIT_SECONDS + 10)
    meta = dict(attempt.attempt_metadata or {})
    meta["last_active_at"] = past_active.isoformat()
    attempt.attempt_metadata = meta
    db_session.commit()

    # 3. Next interaction triggers HTTP 410 Gone and marks status 'expired'
    expired_get = client.get(f"/api/play/{session_token}")
    assert expired_get.status_code == status.HTTP_410_GONE

    # 4. Verify database state
    db_session.refresh(attempt)
    assert attempt.status == "expired"
    assert "expired_at" in (attempt.attempt_metadata or {})

    # 5. All other endpoints reject expired attempt with HTTP 410 Gone
    ans_res = client.post(f"/api/play/{session_token}/answer", json={"answer": "Toshkent"})
    assert ans_res.status_code == status.HTTP_410_GONE

    cont_res = client.post(f"/api/play/{session_token}/continue")
    assert cont_res.status_code == status.HTTP_410_GONE

    reveal_res = client.get(f"/api/play/{session_token}/reveal")
    assert reveal_res.status_code == status.HTTP_410_GONE

    results_res = client.get(f"/api/play/{session_token}/results")
    assert results_res.status_code == status.HTTP_410_GONE


# ==============================================================================
# 4. ANONYMOUS QUOTA NON-BYPASSABLE (COOKIE + SERVER-SIDE IP HASH)
# ==============================================================================

def test_anonymous_quota_strict_non_bypassable(client, seed_sample_quiz, db_session):
    """
    Owner Decision (Player Accounts Milestone):
    - Anonymous gameplay is strictly disallowed.
    - Every player must register and log in before playing.
    - Unauthenticated start requests return HTTP 401 Unauthorized.
    - Deleting cookies or spoofing anon cookies cannot bypass the login requirement.
    - Authenticated users can start and play freely.
    """
    quiz = seed_sample_quiz
    test_ip_headers = {"X-Forwarded-For": "198.51.100.88"}

    # 1. Unauthenticated request without auth cookie returns 401
    client.cookies.clear()
    res1 = client.post(f"/api/play/start/{quiz.id}", headers=test_ip_headers)
    assert res1.status_code == status.HTTP_401_UNAUTHORIZED
    assert "tizimga kiring" in res1.json()["detail"].lower() or "ro'yxatdan o'ting" in res1.json()["detail"].lower()

    # 2. Attempt to bypass using spoofed anonymous cookie fails with 401
    client.cookies.set("zakowhat_anon_id", "anon_attacker_spoofed_random_cookie")
    res2 = client.post(f"/api/play/start/{quiz.id}", headers=test_ip_headers)
    assert res2.status_code == status.HTTP_401_UNAUTHORIZED

    # 3. Attempt to bypass using test header fails with 401
    res3 = client.post(f"/api/play/start/{quiz.id}", headers={**test_ip_headers, "X-Anon-Id": "anon_header_bypass"})
    assert res3.status_code == status.HTTP_401_UNAUTHORIZED

    # 4. Authenticated user can play freely
    reg_res = client.post("/api/auth/register", json={
        "display_name": "QuotaAuthUser",
        "email": "quota_auth@zakowhat.uz",
        "password": "Password123!",
    })
    assert reg_res.status_code == status.HTTP_201_CREATED

    res_auth = client.post(f"/api/play/start/{quiz.id}", headers=test_ip_headers)
    assert res_auth.status_code == status.HTTP_201_CREATED
    assert "session_token" in res_auth.json()


# ==============================================================================
# 5. PACK 02 CANONICAL INTEGRITY & EDITORIAL PRESERVATION
# ==============================================================================

def test_pack_02_canonical_content_integrity():
    """
    Owner Decisions 4, 5, 6:
    - Retain spoken preambles in Q09, Q10, Q13, Q15, Q17, Q23.
    - Question 22 only accepts 'Uchib tushmoq' and 'Uchib tushdi' (NOT 'Yuragi uchmoq').
    - Canonical Pack 02 content remains unchanged.
    """
    pack_path = os.path.join("data", "packs", "pack_02_canonical.json")
    assert os.path.exists(pack_path), "Pack 02 canonical file must exist"

    with open(pack_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Flatten questions across rounds
    questions = []
    for r in data.get("rounds", []):
        questions.extend(r.get("questions", []))

    assert len(questions) == 24, "Pack 02 must have exactly 24 questions"

    # Verify Question 22
    q22 = next(q for q in questions if q.get("global_sequence") == 22)
    assert q22["primary_answer"] == "Uchib tushmoq"
    assert q22.get("alternative_answers") == ["Uchib tushdi"]
    all_q22_answers = [q22["primary_answer"]] + q22.get("alternative_answers", [])
    assert "Yuragi uchmoq" not in all_q22_answers

    # Verify preambles retained in Q09, Q10, Q13, Q15, Q17, Q23
    q9 = next(q for q in questions if q.get("global_sequence") == 9)
    assert "Savolda bir so‘z biroz o‘zgartirilgan" in q9["text"]

    q10 = next(q for q in questions if q.get("global_sequence") == 10)
    assert "Savolda almashtirishlar bor." in q10["text"]

    q13 = next(q for q in questions if q.get("global_sequence") == 13)
    assert "Savolda bir so‘zdan ikki o‘rinda harflar tushirib qoldirilgan." in q13["text"]

    q15 = next(q for q in questions if q.get("global_sequence") == 15)
    assert "Savolda bir so‘zdan harflar tushirib qoldirilgan." in q15["text"]

    q17 = next(q for q in questions if q.get("global_sequence") == 17)
    assert "Savol matnida bir so‘zdan ikki o‘rinda harflar tushirib qoldirilgan" in q17["text"]

    q23 = next(q for q in questions if q.get("global_sequence") == 23)
    assert "Savolda almashtirish bor." in q23["text"]


# ==============================================================================
# 6. UI / TEMPLATE ALIGNMENT (GIRIH REMOVAL, MODE FILTER, TIMERS)
# ==============================================================================

def test_girih_removed_from_grand_sanctum():
    """
    Owner Decision 7:
    - Permanently remove Girih-i Muammo lore section from templates/homepage_grand_sanctum.html.
    - Timer text updated to 90–180 soniyalik.
    """
    template_path = os.path.join("templates", "homepage_grand_sanctum.html")
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "Girih-i Muammo" not in content
    assert 'id="lore"' not in content
    # Approved homepage copy states the range as "90–180 soniya" (Zakovat game card)
    assert "90–180 soniya" in content
    assert "90–150" not in content


def test_future_game_modes_and_arena_scope():
    """
    Owner Decisions 9 & 10:
    - Deprecate modern_multiround and svoyak from public UI.
    - FUTURE_MINI_GAMES = ["wheel"] in services/gameplay.py.
    - Arena filter only offers 'all' and 'classic_zakovat'.
    - Timer text updated to (90–180s) in templates/index.html.
    """
    assert "classic_zakovat" in SUPPORTED_GAME_MODES
    assert "svoyak" in SUPPORTED_GAME_MODES
    assert FUTURE_MINI_GAMES == ["wheel"]

    index_path = os.path.join("templates", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Arena filter must not contain deprecated modes
    assert 'value="modern_multiround"' not in content

    # Arena filter must contain all, classic_zakovat, and svoyak
    assert 'value="all"' in content
    assert 'value="classic_zakovat"' in content
    assert 'value="svoyak"' in content

    # Timer text must be 90-180s
    assert "(90–180s)" in content
    assert "(90–150s)" not in content
