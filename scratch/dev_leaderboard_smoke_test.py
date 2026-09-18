import sys
import os
sys.path.insert(0, os.path.abspath("."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
from database import SessionLocal
from main import app
from models import Question, AcceptedAnswer, Quiz, QuizVersion, Round, RoundQuestion, SoloAttempt, AnswerRecord, User


def run_dev_leaderboard_smoke_test():
    print("=== STARTING LIVE NEON DEV SMOKE TEST FOR GAMEPLAY & LEADERBOARD MILESTONE ===")
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

        # Step 2: Create & Publish Public Quiz and Unlisted Quiz
        print("\n[Step 2] Creating and publishing Public and Unlisted quizzes...")
        # 2a. Public Quiz
        d1_res = client.post("/api/drafts", json={"title": "DEV Public Championship 2026", "game_mode": "modern_multiround"})
        assert d1_res.status_code == 201
        d1_id = d1_res.json()["version_id"]
        quiz1_id = d1_res.json()["quiz_id"]

        r1_res = client.post(f"/api/drafts/{d1_id}/rounds", json={
            "round_type": "mantiqqasqon",
            "config": {"hidden_rule": "Barcha javoblar O'zbekiston viloyatlari markazlari"}
        })
        r1_id = r1_res.json()["round_id"]

        client.post(f"/api/drafts/{d1_id}/rounds/{r1_id}/questions", json={
            "text": "Samarqand viloyatining ma'muriy markazi qaysi shahar?",
            "primary_answer": "Samarqand",
            "points": 1,
            "explanation": "Samarqand - qadimiy Registon maydoniga ega shahar",
        })
        client.post(f"/api/drafts/{d1_id}/rounds/{r1_id}/questions", json={
            "text": "Farg'ona viloyatining ma'muriy markazi qaysi shahar?",
            "primary_answer": "Farg'ona",
            "points": 1,
            "explanation": "Farg'ona vodiysining go'zal shaharlaridan biri",
        })

        pub1_res = client.post(f"/api/drafts/{d1_id}/publish", json={"visibility": "public"})
        assert pub1_res.status_code == 200
        print(f"[OK] Public quiz published: id={quiz1_id}")

        # 2b. Unlisted Quiz
        d2_res = client.post("/api/drafts", json={"title": "DEV Unlisted Secret Cup", "game_mode": "modern_multiround"})
        assert d2_res.status_code == 201
        d2_id = d2_res.json()["version_id"]
        quiz2_id = d2_res.json()["quiz_id"]

        r2_res = client.post(f"/api/drafts/{d2_id}/rounds", json={"round_type": "standard"})
        r2_id = r2_res.json()["round_id"]
        client.post(f"/api/drafts/{d2_id}/rounds/{r2_id}/questions", json={
            "text": "Yorug'lik tezligi vakuumda necha km/s?",
            "primary_answer": "300000",
            "points": 1,
        })

        pub2_res = client.post(f"/api/drafts/{d2_id}/publish", json={"visibility": "unlisted"})
        assert pub2_res.status_code == 200
        print(f"[OK] Unlisted quiz published: id={quiz2_id}")

        # Step 3: Verify Discovery Filtering & Direct Play
        print("\n[Step 3] Testing discovery visibility vs direct link access...")
        disc_res = client.get("/api/quizzes")
        assert disc_res.status_code == 200
        disc_ids = [q["quiz_id"] for q in disc_res.json()]
        assert quiz1_id in disc_ids, "Public quiz must be in discovery"
        assert quiz2_id not in disc_ids, "Unlisted quiz MUST NOT be in discovery"
        print("[OK] Discovery accurately filters out unlisted quiz.")

        # Play unlisted via direct link
        client.cookies.clear()
        unlisted_play = client.post(f"/api/play/start/{quiz2_id}")
        assert unlisted_play.status_code == 201, "Unlisted quiz must be playable via direct link"
        print("[OK] Direct play for unlisted quiz verified.")

        # Step 4: Anonymous Single-Attempt Gate & 403 Blocking
        print("\n[Step 4] Testing Anonymous 1-attempt gate & second attempt blocking...")
        anon_client = TestClient(app)
        start_anon = anon_client.post(f"/api/play/start/{quiz1_id}")
        assert start_anon.status_code == 201
        anon_token = start_anon.json()["session_token"]
        assert "zakowhat_anon_id" in anon_client.cookies
        print("[OK] Anonymous visitor started attempt #1, anon cookie issued.")

        # Attempt to start a second quiz with same anon cookie
        second_start = anon_client.post(f"/api/play/start/{quiz1_id}")
        assert second_start.status_code == 403, f"Expected 403, got {second_start.status_code}"
        assert "bepul urinishdan foydalanildi" in second_start.json()["detail"].lower()
        print("[OK] Second attempt correctly rejected with HTTP 403.")

        # Step 5: Complete Gameplay & Verify Results + Review
        print("\n[Step 5] Playing through quiz to completion...")
        # Q1: Correct answer
        ans1 = anon_client.post(f"/api/play/{anon_token}/answer", json={"answer": "samarqand"})
        assert ans1.status_code == 200
        assert ans1.json()["is_correct"] is True

        # Q2: Incorrect answer
        ans2 = anon_client.post(f"/api/play/{anon_token}/answer", json={"answer": "noto'g'ri"})
        assert ans2.status_code == 200
        assert ans2.json()["is_correct"] is False
        assert ans2.json()["round_completed"] is True

        # Reveal screen
        rev = anon_client.get(f"/api/play/{anon_token}/reveal").json()
        assert rev["config"]["hidden_rule"] == "Barcha javoblar O'zbekiston viloyatlari markazlari"
        print("[OK] Mantiqqasqon hidden rule safely revealed at round reveal.")

        # Continue to finish
        cont = anon_client.post(f"/api/play/{anon_token}/continue").json()
        assert cont["quiz_completed"] is True
        print("[OK] Quiz completed.")

        # Check results
        results = anon_client.get(f"/api/play/{anon_token}/results").json()
        assert results["is_anonymous"] is True
        assert results["total_correct"] == 1
        assert "formatted_time" in results
        print(f"[OK] Results: correct={results['total_correct']}/{results['total_questions']}, time={results['formatted_time']}")

        # Check review
        review = anon_client.get(f"/api/play/{anon_token}/review").json()
        assert review["total_correct"] == 1
        assert len(review["rounds"][0]["questions"]) == 2
        print("[OK] Completed answer review verified.")

        # Leaderboard check: anonymous attempts must NOT be listed
        lb_anon = anon_client.get(f"/api/quizzes/{quiz1_id}/leaderboard").json()
        assert len(lb_anon["leaderboard"]) == 0
        assert lb_anon["total_participants"] == 0
        print("[OK] Anonymous attempt is strictly excluded from leaderboard.")

        # Step 6: Register User and Auto-Claim Attempt
        print("\n[Step 6] Registering user and auto-claiming anonymous attempt...")

        # 6a. Attacker without matching anon cookie cannot steal this session
        attacker_client = TestClient(app)
        attacker_client.cookies.set("zakowhat_anon_id", "anon_imposter_999")
        steal_res = attacker_client.post("/api/auth/register", json={
            "display_name": "Attacker",
            "email": "attacker@devtest.uz",
            "password": "strongpassword2026",
            "session_token": anon_token,
        })
        assert steal_res.status_code == 400, f"Expected 400, got {steal_res.status_code}"
        assert "anonim egasi mos kelmadi" in steal_res.json()["detail"].lower()
        print("[OK] Attacker attempt to claim session rejected with HTTP 400.")

        # 6b. Legitimate anonymous player with matching cookie claims successfully
        reg_res = anon_client.post("/api/auth/register", json={
            "display_name": "Alisher_Navoiy",
            "email": "alisher@devtest.uz",
            "password": "strongpassword2026",
            "session_token": anon_token,
        })
        assert reg_res.status_code == 201
        reg_data = reg_res.json()
        assert "access_token" not in reg_data, "Raw JWT must not be exposed in response body"
        assert reg_data["attempt_claimed"] is True
        user_id = reg_data["user"]["id"]
        print(f"[OK] User registered (id={user_id}) and attempt claimed successfully (JWT hidden).")

        # Step 7: Verify Leaderboard Ranking
        print("\n[Step 7] Verifying leaderboard shows claimed attempt...")
        lb_claimed = anon_client.get(f"/api/quizzes/{quiz1_id}/leaderboard").json()
        assert len(lb_claimed["leaderboard"]) == 1
        entry = lb_claimed["leaderboard"][0]
        assert entry["display_name"] == "Alisher_Navoiy"
        assert entry["correct_count"] == 1
        assert entry["rank"] == 1
        print(f"[OK] Leaderboard rank #1: {entry['display_name']} ({entry['correct_count']} to'g'ri, {entry['formatted_time']}).")

        # Step 8: Registered User Plays Second Time (Unlimited Attempts)
        print("\n[Step 8] Registered user plays a 2nd time (unlimited attempts & best score)...")
        start_user2 = anon_client.post(f"/api/play/start/{quiz1_id}")
        assert start_user2.status_code == 201, "Registered user must be allowed second attempt"
        token2 = start_user2.json()["session_token"]

        # User scores 2/2 on this attempt!
        anon_client.post(f"/api/play/{token2}/answer", json={"answer": "samarqand"})
        anon_client.post(f"/api/play/{token2}/answer", json={"answer": "farg'ona"})
        anon_client.post(f"/api/play/{token2}/continue")

        lb_updated = anon_client.get(f"/api/quizzes/{quiz1_id}/leaderboard").json()
        assert len(lb_updated["leaderboard"]) == 1, "Only ONE best entry per player"
        best_entry = lb_updated["leaderboard"][0]
        assert best_entry["correct_count"] == 2, "Best score must be updated to 2"
        print(f"[OK] Leaderboard preserved best attempt: {best_entry['correct_count']}/2 correct.")

        print("\n[OK] ALL FUNCTIONAL MILESTONE REQUIREMENTS VERIFIED SUCCESSFULLY ON NEON DEV!")

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
                        # If question created for this test, delete it
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
    run_dev_leaderboard_smoke_test()
