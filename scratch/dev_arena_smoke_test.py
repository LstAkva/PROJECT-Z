import sys
import os
sys.path.insert(0, os.path.abspath("."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
from database import SessionLocal
from main import app
from models import Question, AcceptedAnswer, Quiz, QuizVersion, Round, RoundQuestion, SoloAttempt, AnswerRecord, User


def run_dev_arena_smoke_test():
    print("=== STARTING LIVE NEON DEV SMOKE TEST FOR ARENA / DISCOVERY + QUIZ DETAIL ===")
    db = SessionLocal()
    client = TestClient(app)

    try:
        # Step 1: Initial Baseline Verification
        print("\n[Step 1] Verifying initial DEV DB baseline...")
        q_count = db.query(Question).count()
        ready_count = db.query(Question).filter(Question.status == "ready").count()
        needs_review_count = db.query(Question).filter(Question.status == "needs_review").count()
        ans_count = db.query(AcceptedAnswer).count()
        quizzes_count = db.query(Quiz).count()
        users_count = db.query(User).count()
        attempts_count = db.query(SoloAttempt).count()

        print(f"Questions: {q_count} (ready: {ready_count}, needs_review: {needs_review_count})")
        print(f"AcceptedAnswers: {ans_count}")
        print(f"Users: {users_count}, Quizzes: {quizzes_count}, Attempts: {attempts_count}")

        assert q_count == 3284, f"Expected 3284 questions, found {q_count}"
        assert ready_count == 3259, f"Expected 3259 ready questions, found {ready_count}"
        assert needs_review_count == 25, f"Expected 25 needs_review questions, found {needs_review_count}"
        assert ans_count == 3331, f"Expected 3331 answers, found {ans_count}"
        assert quizzes_count == 0, f"Expected 0 quizzes, found {quizzes_count}"
        assert users_count == 0, f"Expected 0 users, found {users_count}"
        assert attempts_count == 0, f"Expected 0 attempts, found {attempts_count}"
        print("[OK] Baseline confirmed on Neon DEV database.")

        # Step 2: Test Categories Endpoint
        print("\n[Step 2] Testing /api/quizzes/categories...")
        cat_res = client.get("/api/quizzes/categories")
        assert cat_res.status_code == 200
        categories = cat_res.json()["categories"]
        assert "Tarix" in categories
        assert "Fan va Texnologiya" in categories
        assert "Umumiy" in categories
        print(f"[OK] Categories returned: {categories}")

        # Step 3: Create & Publish Quizzes (Public Tarix, Public Fan, Unlisted Mantiq, Draft)
        print("\n[Step 3] Creating and publishing test quizzes...")

        # 3a. Quiz 1: Public, Tarix, modern_multiround
        d1_res = client.post("/api/drafts", json={
            "title": "DEV Amir Temur Saltanati",
            "description": "Temuriy hukmdorlar va o'zbek davlatchiligi",
            "game_mode": "modern_multiround"
        })
        assert d1_res.status_code == 201
        d1_id = d1_res.json()["version_id"]
        quiz1_id = d1_res.json()["quiz_id"]

        r1_res = client.post(f"/api/drafts/{d1_id}/rounds", json={"round_type": "standard"})
        r1_id = r1_res.json()["round_id"]
        client.post(f"/api/drafts/{d1_id}/rounds/{r1_id}/questions", json={
            "text": "Amir Temur nechanchi yilda tavallud topgan?",
            "primary_answer": "1336",
            "points": 1,
            "explanation": "Amir Temur 1336-yil 9-aprelda Xo'ja Ilg'or qishlog'ida tug'ilgan",
        })
        client.post(f"/api/drafts/{d1_id}/rounds/{r1_id}/questions", json={
            "text": "Temuriylar davlatining poytaxti qaysi shahar bo'lgan?",
            "primary_answer": "Samarqand",
            "points": 1,
            "explanation": "Samarqand saltanat poytaxti bo'lgan",
        })

        pub1_res = client.post(f"/api/drafts/{d1_id}/publish", json={"visibility": "public", "category": "Tarix"})
        assert pub1_res.status_code == 200
        print(f"[OK] Quiz 1 published (Public, Tarix): {quiz1_id}")

        # 3b. Quiz 2: Public, Fan va Texnologiya, blitz
        d2_res = client.post("/api/drafts", json={"title": "DEV Kvant Fizikasi Asoslari", "game_mode": "blitz"})
        assert d2_res.status_code == 201
        d2_id = d2_res.json()["version_id"]
        quiz2_id = d2_res.json()["quiz_id"]

        r2_res = client.post(f"/api/drafts/{d2_id}/rounds", json={"round_type": "standard"})
        assert r2_res.status_code == 201
        r2_id = r2_res.json()["round_id"]
        client.post(f"/api/drafts/{d2_id}/rounds/{r2_id}/questions", json={
            "text": "Yorug'lik kvantining nomi nima?",
            "primary_answer": "Foton",
            "points": 1,
            "explanation": "Foton elektromagnit nurlanishning kvanti hisoblanadi",
        })

        pub2_res = client.post(f"/api/drafts/{d2_id}/publish", json={"visibility": "public", "category": "Fan va Texnologiya"})
        assert pub2_res.status_code == 200
        print(f"[OK] Quiz 2 published (Public, Fan va Texnologiya): {quiz2_id}")

        # 3c. Quiz 3: Unlisted, Mantiq va Qiziqarli, svoyak
        d3_res = client.post("/api/drafts", json={"title": "DEV Maxfiy Mantiqiy Bellashuv", "game_mode": "svoyak"})
        assert d3_res.status_code == 201
        d3_id = d3_res.json()["version_id"]
        quiz3_id = d3_res.json()["quiz_id"]

        r3_res = client.post(f"/api/drafts/{d3_id}/rounds", json={"round_type": "svoyak_theme"})
        assert r3_res.status_code == 201
        r3_id = r3_res.json()["round_id"]
        client.post(f"/api/drafts/{d3_id}/rounds/{r3_id}/questions", json={
            "text": "Bir yil ichida necha oyda 30 kun bor?",
            "primary_answer": "11",
            "points": 10,
            "explanation": "Fevraldan boshqa barcha 11 oyda kamida 30 kun bor",
        })

        pub3_res = client.post(f"/api/drafts/{d3_id}/publish", json={"visibility": "unlisted", "category": "Mantiq va Qiziqarli"})
        assert pub3_res.status_code == 200
        print(f"[OK] Quiz 3 published (Unlisted): {quiz3_id}")

        # 3d. Quiz 4: Draft only (never published)
        d4_res = client.post("/api/drafts", json={"title": "DEV Tugallanmagan Qoralama", "game_mode": "standard"})
        assert d4_res.status_code == 201
        quiz4_id = d4_res.json()["quiz_id"]
        print(f"[OK] Quiz 4 created (Draft only): {quiz4_id}")

        # Step 4: Verify Arena Listing & Visibility Invariants
        print("\n[Step 4] Verifying Arena listing and visibility rules...")
        arena_res = client.get("/api/quizzes")
        assert arena_res.status_code == 200
        arena_list = arena_res.json()
        arena_ids = [q["quiz_id"] for q in arena_list]

        assert quiz1_id in arena_ids, "Quiz 1 (public) must appear in Arena"
        assert quiz2_id in arena_ids, "Quiz 2 (public) must appear in Arena"
        assert quiz3_id not in arena_ids, "Quiz 3 (unlisted) MUST NOT appear in Arena"
        assert quiz4_id not in arena_ids, "Quiz 4 (draft) MUST NOT appear in Arena"
        print("[OK] Visibility invariants verified: only published public quizzes appear.")

        # Check card structure on Quiz 1
        q1_card = next(q for q in arena_list if q["quiz_id"] == quiz1_id)
        assert q1_card["title"] == "DEV Amir Temur Saltanati"
        assert q1_card["category"] == "Tarix"
        assert q1_card["game_mode"] == "modern_multiround"
        assert q1_card["total_rounds"] == 1
        assert q1_card["total_questions"] == 2
        assert q1_card["estimated_duration_minutes"] == 2
        assert q1_card["top_player"] is None
        print(f"[OK] Quiz card fields verified: {q1_card}")

        # Step 5: Test Search and Filtering in Discovery
        print("\n[Step 5] Testing Arena Search and Filters...")
        # 5a. Text search (q)
        search_res = client.get("/api/quizzes", params={"q": "Amir Temur"})
        assert search_res.status_code == 200
        s_ids = [q["quiz_id"] for q in search_res.json()]
        assert s_ids == [quiz1_id], f"Expected only Quiz 1 for 'Amir Temur', got {s_ids}"

        # 5b. Game mode filter
        mode_res = client.get("/api/quizzes", params={"game_mode": "blitz"})
        assert mode_res.status_code == 200
        m_ids = [q["quiz_id"] for q in mode_res.json()]
        assert m_ids == [quiz2_id], f"Expected only Quiz 2 for 'blitz', got {m_ids}"

        # 5c. Category filter
        cat_filter_res = client.get("/api/quizzes", params={"category": "Tarix"})
        assert cat_filter_res.status_code == 200
        c_ids = [q["quiz_id"] for q in cat_filter_res.json()]
        assert c_ids == [quiz1_id], f"Expected only Quiz 1 for 'Tarix', got {c_ids}"

        # 5d. Combined filters
        comb_res = client.get("/api/quizzes", params={"q": "Kvant", "game_mode": "blitz", "category": "Fan va Texnologiya"})
        assert comb_res.status_code == 200
        comb_ids = [q["quiz_id"] for q in comb_res.json()]
        assert comb_ids == [quiz2_id], f"Expected Quiz 2 for combined filter, got {comb_ids}"

        # 5e. Non-matching query
        no_res = client.get("/api/quizzes", params={"q": "MavjudBo'lmaganQuiz999"})
        assert no_res.status_code == 200
        assert len(no_res.json()) == 0

        # 5f. Uzbek apostrophe variants search (curly ‘ and modifier ʻ)
        uz_res1 = client.get("/api/quizzes", params={"q": "o‘zbek"}).json()
        assert any(q["quiz_id"] == quiz1_id for q in uz_res1), "Search with curly quote ‘ must find Quiz 1"
        uz_res2 = client.get("/api/quizzes", params={"q": "oʻzbek"}).json()
        assert any(q["quiz_id"] == quiz1_id for q in uz_res2), "Search with modifier comma ʻ must find Quiz 1"
        print("[OK] Search and filtering (including Uzbek apostrophe variants) verified across all dimensions.")

        # Step 6: Test Quiz Detail Endpoint
        print("\n[Step 6] Testing Quiz Detail endpoint (/api/quizzes/{id})...")
        d_res = client.get(f"/api/quizzes/{quiz1_id}")
        assert d_res.status_code == 200
        d_data = d_res.json()
        assert d_data["quiz_id"] == quiz1_id
        assert d_data["title"] == "DEV Amir Temur Saltanati"
        assert d_data["category"] == "Tarix"
        assert d_data["visibility"] == "public"
        assert d_data["total_rounds"] == 1
        assert d_data["total_questions"] == 2
        assert d_data["estimated_duration_minutes"] == 2
        assert len(d_data["rounds_summary"]) == 1
        assert d_data["rounds_summary"][0]["questions_count"] == 2
        assert d_data["top_player"] is None
        assert d_data["leaderboard_preview"] == []
        print(f"[OK] Quiz Detail structure verified: {d_data['title']}, rounds={d_data['total_rounds']}, questions={d_data['total_questions']}")

        # Test Quiz Detail for Unlisted Quiz (accessible via direct link)
        unlisted_detail = client.get(f"/api/quizzes/{quiz3_id}")
        assert unlisted_detail.status_code == 200
        assert unlisted_detail.json()["visibility"] == "unlisted"
        print("[OK] Unlisted quiz detail accessible via direct ID.")

        # Step 7: Test Play Now flow & Anonymous Attempt
        print("\n[Step 7] Testing Play Now flow and anonymous gameplay...")
        anon_client = TestClient(app)
        start_res = anon_client.post(f"/api/play/start/{quiz1_id}")
        assert start_res.status_code == 201
        session_data = start_res.json()
        token = session_data["session_token"]
        assert session_data["quiz_title"] == "DEV Amir Temur Saltanati"
        print(f"[OK] Play started successfully for Quiz 1: token={token[:16]}...")

        # Answer questions
        ans1 = anon_client.post(f"/api/play/{token}/answer", json={"answer": "1336"})
        assert ans1.status_code == 200 and ans1.json()["is_correct"] is True

        ans2 = anon_client.post(f"/api/play/{token}/answer", json={"answer": "Samarqand"})
        assert ans2.status_code == 200 and ans2.json()["is_correct"] is True

        cont_res = anon_client.post(f"/api/play/{token}/continue")
        assert cont_res.status_code == 200 and cont_res.json()["quiz_completed"] is True
        print("[OK] Anonymous player scored 2/2 and completed quiz.")

        # Step 8: User Registration, Session Claim, Leaderboard Preview Update
        print("\n[Step 8] Registering user, claiming attempt, and verifying detail leaderboard preview...")
        reg_res = anon_client.post("/api/auth/register", json={
            "display_name": "Ulugbek_Yulduz",
            "email": "ulugbek@devtest.uz",
            "password": "strongpassword2026",
            "session_token": token,
        })
        assert reg_res.status_code == 201
        assert reg_res.json()["attempt_claimed"] is True
        print("[OK] User registered and anonymous session claimed.")

        # Now re-check Quiz Detail for Quiz 1
        d_after = client.get(f"/api/quizzes/{quiz1_id}").json()
        assert d_after["top_player"]["display_name"] == "Ulugbek_Yulduz"
        assert len(d_after["leaderboard_preview"]) == 1
        preview_entry = d_after["leaderboard_preview"][0]
        assert preview_entry["rank"] == 1
        assert preview_entry["display_name"] == "Ulugbek_Yulduz"
        assert preview_entry["correct_count"] == 2
        print(f"[OK] Quiz Detail now reflects top player: {preview_entry['display_name']} with {preview_entry['correct_count']}/2 correct.")

        # Re-check Arena card for Quiz 1
        arena_after = client.get("/api/quizzes").json()
        card_after = next(q for q in arena_after if q["quiz_id"] == quiz1_id)
        assert card_after["top_player"]["display_name"] == "Ulugbek_Yulduz"
        print(f"[OK] Arena quiz card now reflects top player: {card_after['top_player']['display_name']}.")

        print("\n[OK] ALL ARENA / DISCOVERY + QUIZ DETAIL VERIFICATIONS PASSED SUCCESSFULLY ON NEON DEV!")

    finally:
        # Step 9: Clean Up and Restore DEV Baseline
        print("\n[Step 9] Cleaning up test data to restore Neon DEV baseline...")
        # Delete attempts and records
        test_attempts = db.query(SoloAttempt).filter(
            SoloAttempt.quiz_version_id.in_(
                db.query(QuizVersion.id).filter(
                    QuizVersion.quiz_id.in_(
                        db.query(Quiz.id).filter(Quiz.title.like("DEV %"))
                    )
                )
            )
        ).all()
        for att in test_attempts:
            db.query(AnswerRecord).filter(AnswerRecord.attempt_id == att.id).delete()
            db.delete(att)
        db.flush()

        # Delete any remaining attempts
        db.query(AnswerRecord).delete()
        db.query(SoloAttempt).delete()

        # Delete test users
        db.query(User).filter(User.email.like("%@devtest.uz")).delete()
        db.query(User).delete()

        # Delete test round questions, rounds, quiz versions, and quizzes
        test_quizzes = db.query(Quiz).filter(Quiz.title.like("DEV %")).all()
        for qz in test_quizzes:
            versions = db.query(QuizVersion).filter(QuizVersion.quiz_id == qz.id).all()
            for v in versions:
                for r in v.rounds:
                    rqs = db.query(RoundQuestion).filter(RoundQuestion.round_id == r.id).all()
                    for rq in rqs:
                        q_id = rq.question_id
                        db.delete(rq)
                        db.flush()
                        db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q_id).delete()
                        db.query(Question).filter(Question.id == q_id).delete()
                    db.delete(r)
                db.delete(v)
            db.delete(qz)

        db.commit()

        # Verify baseline restored
        final_q = db.query(Question).count()
        final_ans = db.query(AcceptedAnswer).count()
        final_quizzes = db.query(Quiz).count()
        final_users = db.query(User).count()
        final_attempts = db.query(SoloAttempt).count()

        print(f"\nFinal State on Neon DEV: Questions={final_q}, Answers={final_ans}, Users={final_users}, Quizzes={final_quizzes}, Attempts={final_attempts}")
        assert final_q == 3284, f"Expected 3284 questions, got {final_q}"
        assert final_ans == 3331, f"Expected 3331 answers, got {final_ans}"
        assert final_users == 0, f"Expected 0 users, got {final_users}"
        assert final_quizzes == 0, f"Expected 0 quizzes, got {final_quizzes}"
        assert final_attempts == 0, f"Expected 0 attempts, got {final_attempts}"
        print("[SUCCESS] NEON DEV DATABASE BASELINE 100% RESTORED!")
        db.close()


if __name__ == "__main__":
    run_dev_arena_smoke_test()
