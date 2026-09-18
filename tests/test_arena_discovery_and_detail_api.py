import pytest
from fastapi import status
from datetime import datetime, timezone
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer, SoloAttempt, User


def create_and_publish_quiz(
    client,
    title="Test Tournament",
    description="Test tournament description",
    game_mode="modern_multiround",
    visibility="public",
    category="Umumiy",
    questions_count=2,
):
    """Helper to create a valid draft and publish it with given parameters."""
    res = client.post("/api/drafts", json={"title": title, "description": description, "game_mode": game_mode})
    assert res.status_code == status.HTTP_201_CREATED
    draft_id = res.json()["version_id"]

    # Create round
    r_res = client.post(f"/api/drafts/{draft_id}/rounds", json={"round_type": "standard"})
    assert r_res.status_code == status.HTTP_201_CREATED
    round_id = r_res.json()["round_id"]

    for i in range(1, questions_count + 1):
        q_res = client.post(f"/api/drafts/{draft_id}/rounds/{round_id}/questions", json={
            "text": f"{title} Q{i}",
            "primary_answer": f"Ans{i}",
            "points": 1,
            "explanation": f"Explanation for Q{i}",
        })
        assert q_res.status_code == status.HTTP_201_CREATED

    pub_res = client.post(f"/api/drafts/{draft_id}/publish", json={
        "visibility": visibility,
        "category": category,
    })
    assert pub_res.status_code == status.HTTP_200_OK
    data = pub_res.json()
    return data["quiz_id"], data["version_id"]


def test_public_quizzes_appear_in_arena(client):
    """1. Public quizzes appear in Arena."""
    quiz_id, _ = create_and_publish_quiz(client, title="Arena Ochiq Turniri", visibility="public")
    res = client.get("/api/quizzes")
    assert res.status_code == status.HTTP_200_OK
    quizzes = res.json()
    found = next((q for q in quizzes if q["quiz_id"] == quiz_id), None)
    assert found is not None
    assert found["title"] == "Arena Ochiq Turniri"
    assert found["visibility"] == "public"
    assert found["total_questions"] == 2
    assert found["total_rounds"] == 1
    assert "estimated_duration_minutes" in found


def test_unlisted_quizzes_do_not_appear_in_arena(client):
    """2. Unlisted quizzes do not appear in Arena discovery."""
    quiz_id, _ = create_and_publish_quiz(client, title="Maxfiy Unlisted Kviz", visibility="unlisted")
    res = client.get("/api/quizzes")
    assert res.status_code == status.HTTP_200_OK
    quizzes = res.json()
    found = next((q for q in quizzes if q["quiz_id"] == quiz_id), None)
    assert found is None, "Unlisted quiz must not appear in public Arena discovery"


def test_draft_quizzes_do_not_appear(client):
    """3. Draft quizzes do not appear in Arena or Detail."""
    res = client.post("/api/drafts", json={"title": "Tugallanmagan Qoralama", "game_mode": "modern_multiround"})
    assert res.status_code == status.HTTP_201_CREATED
    draft_quiz_id = res.json()["quiz_id"]

    # Not in Arena list
    disc_res = client.get("/api/quizzes")
    assert disc_res.status_code == status.HTTP_200_OK
    quizzes = disc_res.json()
    assert not any(q["quiz_id"] == draft_quiz_id for q in quizzes)

    # Detail returns 404
    detail_res = client.get(f"/api/quizzes/{draft_quiz_id}")
    assert detail_res.status_code == status.HTTP_404_NOT_FOUND


def test_text_search(client):
    """4. Search works on title and description."""
    q1_id, _ = create_and_publish_quiz(client, title="Temuriylar Davlati Tarixi", description="Buyuk sarkarda Amir Temur haqida")
    q2_id, _ = create_and_publish_quiz(client, title="Zamonaviy Kvant Fizikasi", description="Elementar zarrachalar va yadro")

    # Search by title keyword
    s1 = client.get("/api/quizzes?q=temuriylar").json()
    s1_ids = [q["quiz_id"] for q in s1]
    assert q1_id in s1_ids
    assert q2_id not in s1_ids

    # Search by description keyword
    s2 = client.get("/api/quizzes?q=zarrachalar").json()
    s2_ids = [q["quiz_id"] for q in s2]
    assert q2_id in s2_ids
    assert q1_id not in s2_ids

    # Search non-matching
    s3 = client.get("/api/quizzes?q=mavjud_bolmagan_soz_xyz").json()
    assert len(s3) == 0


def test_game_mode_filtering(client):
    """5. Game-mode filtering works."""
    q_multi, _ = create_and_publish_quiz(client, title="Multi Game", game_mode="modern_multiround")
    q_svoyak, _ = create_and_publish_quiz(client, title="Svoyak Game", game_mode="svoyak")

    res_svoyak = client.get("/api/quizzes?game_mode=svoyak").json()
    svoyak_ids = [q["quiz_id"] for q in res_svoyak]
    assert q_svoyak in svoyak_ids
    assert q_multi not in svoyak_ids

    res_multi = client.get("/api/quizzes?game_mode=modern_multiround").json()
    multi_ids = [q["quiz_id"] for q in res_multi]
    assert q_multi in multi_ids
    assert q_svoyak not in multi_ids


def test_category_filtering(client):
    """6. Category filtering works."""
    q_tarix, _ = create_and_publish_quiz(client, title="O'zbekiston Tarixi", category="Tarix")
    q_fan, _ = create_and_publish_quiz(client, title="Informatika Asoslari", category="Fan va Texnologiya")

    res_tarix = client.get("/api/quizzes?category=Tarix").json()
    tarix_ids = [q["quiz_id"] for q in res_tarix]
    assert q_tarix in tarix_ids
    assert q_fan not in tarix_ids

    res_fan = client.get("/api/quizzes?category=Fan va Texnologiya").json()
    fan_ids = [q["quiz_id"] for q in res_fan]
    assert q_fan in fan_ids
    assert q_tarix not in fan_ids


def test_combined_filters(client):
    """7. Combined filters work (search + mode + category)."""
    q_match, _ = create_and_publish_quiz(
        client,
        title="Al-Xorazmiy va Algoritmlar",
        description="Qadimgi matematika va kompyuter fanlari",
        game_mode="modern_multiround",
        category="Fan va Texnologiya",
    )
    q_diff_cat, _ = create_and_publish_quiz(
        client,
        title="Al-Xorazmiy Davri",
        description="Matematika tarixi",
        game_mode="modern_multiround",
        category="Tarix",
    )
    q_diff_mode, _ = create_and_publish_quiz(
        client,
        title="Al-Xorazmiy Tezkor",
        description="Matematika fanidan savollar",
        game_mode="svoyak",
        category="Fan va Texnologiya",
    )

    res = client.get("/api/quizzes?q=algoritm&game_mode=modern_multiround&category=Fan va Texnologiya").json()
    matched_ids = [q["quiz_id"] for q in res]
    assert q_match in matched_ids
    assert q_diff_cat not in matched_ids
    assert q_diff_mode not in matched_ids


def test_quiz_detail_returns_correct_published_version(client):
    """8. Quiz detail returns the correct published version."""
    quiz_id, version_id = create_and_publish_quiz(
        client,
        title="Mukammal Zakovat",
        description="Har tomonlama intellektual jang",
        category="Zakovat",
        game_mode="modern_multiround",
        questions_count=3,
    )

    detail_res = client.get(f"/api/quizzes/{quiz_id}")
    assert detail_res.status_code == status.HTTP_200_OK
    detail = detail_res.json()

    assert detail["quiz_id"] == quiz_id
    assert detail["version_id"] == version_id
    assert detail["title"] == "Mukammal Zakovat"
    assert detail["category"] == "Zakovat"
    assert detail["total_rounds"] == 1
    assert detail["total_questions"] == 3
    assert detail["estimated_duration_minutes"] >= 1
    assert len(detail["rounds"]) == 1
    assert len(detail["rounds"][0]["questions"]) == 3
    assert "accepted_answers" not in detail["rounds"][0]["questions"][0]


def test_quiz_detail_shows_correct_version_leaderboard_preview(client, db_session):
    """9. Quiz detail shows the correct version's leaderboard preview."""
    quiz_id, version_id = create_and_publish_quiz(client, title="Leaderboard Quiz", questions_count=2)

    user1 = User(email="player1@test.uz", display_name="Champion_1", auth_provider="local")
    user2 = User(email="player2@test.uz", display_name="Runner_Up", auth_provider="local")
    db_session.add_all([user1, user2])
    db_session.flush()

    att1 = SoloAttempt(
        quiz_version_id=version_id,
        user_id=user1.id,
        session_token="token_user1_detail",
        status="completed",
        total_correct=2,
        active_time_seconds=15,
        completed_at=datetime.now(timezone.utc),
    )
    att2 = SoloAttempt(
        quiz_version_id=version_id,
        user_id=user2.id,
        session_token="token_user2_detail",
        status="completed",
        total_correct=1,
        active_time_seconds=20,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([att1, att2])
    db_session.commit()

    detail = client.get(f"/api/quizzes/{quiz_id}").json()
    assert detail["top_player"] is not None
    assert detail["top_player"]["display_name"] == "Champion_1"
    assert detail["top_player"]["correct_count"] == 2

    preview = detail["leaderboard_preview"]
    assert len(preview) == 2
    assert preview[0]["display_name"] == "Champion_1"
    assert preview[0]["rank"] == 1
    assert preview[1]["display_name"] == "Runner_Up"
    assert preview[1]["rank"] == 2


def test_quiz_with_no_leaderboard_entries_returns_proper_empty_state(client):
    """10. A quiz with no leaderboard entries returns a proper empty state."""
    quiz_id, _ = create_and_publish_quiz(client, title="Freshly Published Quiz")

    quizzes = client.get("/api/quizzes").json()
    card = next(q for q in quizzes if q["quiz_id"] == quiz_id)
    assert card["top_player"] is None

    detail = client.get(f"/api/quizzes/{quiz_id}").json()
    assert detail["top_player"] is None
    assert detail["leaderboard_preview"] == []


def test_version_1_leaderboard_data_never_leaks_into_version_2(client, db_session):
    """11. Version 1 leaderboard data never leaks into version 2."""
    quiz_id, v1_id = create_and_publish_quiz(client, title="Evolving Quiz v1", questions_count=2)

    u = User(email="v1_champ@test.uz", display_name="V1_Legend", auth_provider="local")
    db_session.add(u)
    db_session.flush()

    att_v1 = SoloAttempt(
        quiz_version_id=v1_id,
        user_id=u.id,
        session_token="v1_token_sample",
        status="completed",
        total_correct=2,
        active_time_seconds=10,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(att_v1)
    db_session.commit()

    v1_lb = client.get(f"/api/quizzes/{quiz_id}/versions/1/leaderboard").json()
    assert len(v1_lb["leaderboard"]) == 1
    assert v1_lb["leaderboard"][0]["display_name"] == "V1_Legend"

    v2_round = Round(quiz_version_id=None, sequence=1, round_type="standard")
    v2_version = QuizVersion(quiz_id=quiz_id, version_number=2, game_mode="modern_multiround", status="draft")
    db_session.add(v2_version)
    db_session.flush()

    v2_round.quiz_version_id = v2_version.id
    db_session.add(v2_round)
    db_session.flush()

    q_v2 = Question(text="V2 Question", points=1)
    db_session.add(q_v2)
    db_session.flush()

    ans_v2 = AcceptedAnswer(question_id=q_v2.id, answer_text="AnsV2", is_primary=True)
    db_session.add(ans_v2)

    rq_v2 = RoundQuestion(round_id=v2_round.id, question_id=q_v2.id, sequence=1)
    db_session.add(rq_v2)
    db_session.commit()

    # Prior to publishing V2, V1 remains active in Discovery and Detail
    mid_detail = client.get(f"/api/quizzes/{quiz_id}").json()
    assert mid_detail["version_number"] == 1, "Draft V2 must not replace published V1!"
    assert mid_detail["top_player"]["display_name"] == "V1_Legend"

    mid_disc = client.get("/api/quizzes").json()
    found = next(q for q in mid_disc if q["quiz_id"] == quiz_id)
    assert found["version_number"] == 1
    assert found["top_player"]["display_name"] == "V1_Legend"

    pub_v2 = client.post(f"/api/drafts/{v2_version.id}/publish", json={"visibility": "public", "category": "Zakovat"})
    assert pub_v2.status_code == status.HTTP_200_OK

    detail_v2 = client.get(f"/api/quizzes/{quiz_id}").json()
    assert detail_v2["version_number"] == 2
    assert detail_v2["top_player"] is None, "V2 has 0 attempts; V1 top player must not leak into V2!"
    assert len(detail_v2["leaderboard_preview"]) == 0

    v2_lb = client.get(f"/api/quizzes/{quiz_id}/versions/2/leaderboard").json()
    assert len(v2_lb["leaderboard"]) == 0


def test_play_now_enters_existing_play_start_flow(client):
    """12. Play Now still enters the existing /api/play/start/{quiz_id} flow."""
    quiz_id, _ = create_and_publish_quiz(client, title="Play Flow Quiz")

    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    data = start_res.json()
    assert "session_token" in data
    assert data["quiz_title"] == "Play Flow Quiz"
    assert "zakowhat_anon_id" in client.cookies


def test_anonymous_attempt_restrictions_remain_unchanged(client):
    """13. Anonymous attempt restrictions remain unchanged."""
    quiz_id, _ = create_and_publish_quiz(client, title="Anon Restriction Quiz")

    res1 = client.post(f"/api/play/start/{quiz_id}")
    assert res1.status_code == status.HTTP_201_CREATED

    res2 = client.post(f"/api/play/start/{quiz_id}")
    assert res2.status_code == status.HTTP_403_FORBIDDEN


def test_unlisted_direct_link_gameplay_remains_functional(client):
    """14. Unlisted direct-link gameplay remains functional."""
    quiz_id, _ = create_and_publish_quiz(client, title="Secret Unlisted Tournament", visibility="unlisted")

    detail_res = client.get(f"/api/quizzes/{quiz_id}")
    assert detail_res.status_code == status.HTTP_200_OK
    assert detail_res.json()["visibility"] == "unlisted"

    client.cookies.clear()
    start_res = client.post(f"/api/play/start/{quiz_id}")
    assert start_res.status_code == status.HTTP_201_CREATED
    token = start_res.json()["session_token"]

    ans_res = client.post(f"/api/play/{token}/answer", json={"answer": "Ans1"})
    assert ans_res.status_code == status.HTTP_200_OK


def test_arena_categories_endpoint(client):
    """15. /api/quizzes/categories returns standard + custom categories."""
    create_and_publish_quiz(client, title="Kosmos Tarixi", category="Koinot")

    cat_res = client.get("/api/quizzes/categories")
    assert cat_res.status_code == status.HTTP_200_OK
    cats = cat_res.json()["categories"]
    assert "Umumiy" in cats
    assert "Zakovat" in cats
    assert "Tarix" in cats
    assert "Adabiyot va San'at" in cats
    assert "Koinot" in cats


def test_arena_search_with_uzbek_apostrophe_variants(client):
    """16. Search accurately handles all Uzbek apostrophe variants (ʻ, ‘, ’, `)."""
    quiz_id, _ = create_and_publish_quiz(client, title="O'zbekiston madaniyati va san'ati", description="Milliy meros")

    # Standard ASCII quote
    r1 = client.get("/api/quizzes?q=o'zbekiston").json()
    assert any(q["quiz_id"] == quiz_id for q in r1)

    # Left curly quote ‘
    r2 = client.get("/api/quizzes?q=o‘zbekiston").json()
    assert any(q["quiz_id"] == quiz_id for q in r2)

    # Modifier letter turned comma ʻ (official Uzbek standard)
    r3 = client.get("/api/quizzes?q=oʻzbekiston").json()
    assert any(q["quiz_id"] == quiz_id for q in r3)

    # Right curly quote ’
    r4 = client.get("/api/quizzes?q=san’at").json()
    assert any(q["quiz_id"] == quiz_id for q in r4)

    # Backtick `
    r5 = client.get("/api/quizzes?q=san`at").json()
    assert any(q["quiz_id"] == quiz_id for q in r5)


def test_arena_game_mode_filter_blitz_and_standard(client):
    """17. Filtering by blitz and standard modes works properly."""
    blitz_id, _ = create_and_publish_quiz(client, title="Tezkor Blitz Turnir", game_mode="blitz")
    std_id, _ = create_and_publish_quiz(client, title="Oddiy Standart Turnir", game_mode="standard")

    blitz_res = client.get("/api/quizzes?game_mode=blitz").json()
    blitz_ids = [q["quiz_id"] for q in blitz_res]
    assert blitz_id in blitz_ids
    assert std_id not in blitz_ids

    std_res = client.get("/api/quizzes?game_mode=standard").json()
    std_ids = [q["quiz_id"] for q in std_res]
    assert std_id in std_ids
    assert blitz_id not in std_ids
