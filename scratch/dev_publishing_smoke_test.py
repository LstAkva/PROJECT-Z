import sys
import os
sys.path.insert(0, os.path.abspath("."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
from database import SessionLocal
from main import app
from models import Question, AcceptedAnswer, Quiz, QuizVersion, Round, RoundQuestion, SoloAttempt, AnswerRecord

def run_dev_smoke_test():
    print("=== STARTING DEV SMOKE TEST FOR PUBLISHING MILESTONE ===")
    db = SessionLocal()
    client = TestClient(app)

    try:
        # Step 1: Verify Initial Baseline
        print("\n[Step 1] Verifying initial DEV DB baseline...")
        q_count = db.query(Question).count()
        ready_count = db.query(Question).filter(Question.status == "ready").count()
        needs_review_count = db.query(Question).filter(Question.status == "needs_review").count()
        ans_count = db.query(AcceptedAnswer).count()
        quizzes_count = db.query(Quiz).count()
        versions_count = db.query(QuizVersion).count()
        rounds_count = db.query(Round).count()
        attempts_count = db.query(SoloAttempt).count()
        records_count = db.query(AnswerRecord).count()

        print(f"Questions: {q_count} (ready: {ready_count}, needs_review: {needs_review_count})")
        print(f"AcceptedAnswers: {ans_count}")
        print(f"Quizzes: {quizzes_count}, Versions: {versions_count}, Rounds: {rounds_count}")
        print(f"Attempts: {attempts_count}, Records: {records_count}")

        assert q_count == 3284, f"Expected 3284 questions, found {q_count}"
        assert ready_count == 3259, f"Expected 3259 ready questions, found {ready_count}"
        assert needs_review_count == 25, f"Expected 25 needs_review questions, found {needs_review_count}"
        assert ans_count == 3331, f"Expected 3331 answers, found {ans_count}"
        assert quizzes_count == 0, f"Expected 0 quizzes, found {quizzes_count}"
        print("[OK] Initial baseline verified successfully.")

        # Step 2: Create Draft
        print("\n[Step 2] Creating draft quiz via API...")
        draft_res = client.post("/api/drafts", json={
            "title": "DEV Smoke Test Quiz 2026",
            "description": "Validation, Preview and Publishing end-to-end test",
            "game_mode": "modern_multiround"
        })
        assert draft_res.status_code == 201, f"Draft creation failed: {draft_res.text}"
        draft_data = draft_res.json()
        draft_id = draft_data["version_id"]
        quiz_id = draft_data["quiz_id"]
        print(f"[OK] Draft created: version_id={draft_id}, quiz_id={quiz_id}")

        # Step 3: Add Rounds & Authored Questions
        print("\n[Step 3] Adding rounds and questions...")
        # Round 1: Mantiqqasqon with hidden_rule
        r1_res = client.post(f"/api/drafts/{draft_id}/rounds", json={
            "round_type": "mantiqqasqon",
            "config": {
                "theme": "Fizika qonunlari",
                "hidden_rule": "Barcha savollar Nyuton mexanikasi bo'yicha"
            }
        })
        assert r1_res.status_code == 201
        r1_id = r1_res.json()["round_id"]

        q1_res = client.post(f"/api/drafts/{draft_id}/rounds/{r1_id}/questions", json={
            "text": "Nyutonning birinchi qonuni nima deb ataladi?",
            "primary_answer": "Inersiya qonuni",
            "accepted_answers": ["Inersiya", "Inersiya qonuni"],
            "explanation": "Tashqi kuch ta'sir etmasa, jism o'z holatini saqlaydi",
            "points": 2
        })
        assert q1_res.status_code == 201
        q1_data = q1_res.json()
        q1_id = q1_data["question_id"]
        print(f"[OK] Round 1 (Mantiqqasqon) created with Question {q1_id}")

        # Round 2: Aldama meni (True/False)
        r2_res = client.post(f"/api/drafts/{draft_id}/rounds", json={
            "round_type": "aldama_meni",
            "config": {}
        })
        assert r2_res.status_code == 201
        r2_id = r2_res.json()["round_id"]

        q2_res = client.post(f"/api/drafts/{draft_id}/rounds/{r2_id}/questions", json={
            "text": "Yorug'lik tezligi vakuumda taxminan 300 000 km/s ga tengmi?",
            "primary_answer": "Ha",
            "accepted_answers": ["Ha", "Rost", "To'g'ri"],
            "correct_boolean": True,
            "explanation": "Yorug'lik tezligi c ≈ 299 792 458 m/s",
            "points": 1
        })
        assert q2_res.status_code == 201
        print("[OK] Round 2 (Aldama meni) created with Question 2")

        # Step 4: Validate Draft
        print("\n[Step 4] Validating draft readiness...")
        val_res = client.post(f"/api/drafts/{draft_id}/validate")
        assert val_res.status_code == 200
        val_data = val_res.json()
        assert val_data["valid"] is True, f"Validation failed: {val_data}"
        assert val_data["total_rounds"] == 2
        assert val_data["total_questions"] == 2
        print("[OK] Publish-readiness validation passed.")

        # Step 5: Test Preview Endpoint
        print("\n[Step 5] Testing preview endpoint...")
        prev_res = client.get(f"/api/drafts/{draft_id}/preview")
        assert prev_res.status_code == 200
        prev_data = prev_res.json()
        assert prev_data["total_rounds"] == 2
        assert prev_data["total_questions"] == 2
        # Verify hidden_rule is STRIPPED from preview
        assert "hidden_rule" not in prev_data["rounds"][0]["config"]
        # Verify answers are STRIPPED
        assert "primary_answer" not in prev_data["rounds"][0]["questions"][0]
        assert "accepted_answers" not in prev_data["rounds"][0]["questions"][0]
        print("[OK] Preview endpoint verified (answers and hidden_rule safely stripped).")

        # Step 6: Test Preview Simulation & Isolation
        print("\n[Step 6] Testing preview simulation and public play isolation...")
        # Public play must reject draft quiz
        pub_play = client.post(f"/api/play/start/{quiz_id}")
        assert pub_play.status_code == 404, "Draft quiz leaked to public play!"

        # Creator preview simulation starts
        sim_res = client.post(f"/api/drafts/{draft_id}/preview/start")
        assert sim_res.status_code == 201
        sim_data = sim_res.json()
        sim_token = sim_data["session_token"]
        assert sim_data["is_preview"] is True
        assert "hidden_rule" not in sim_data["current_state"]["round"]["config"]

        # Submit answer in preview
        sub1 = client.post(f"/api/play/{sim_token}/answer", json={"answer": "Inersiya qonuni"})
        assert sub1.status_code == 200
        assert sub1.json()["is_correct"] is True
        assert sub1.json()["round_completed"] is True

        # Reveal in preview -> now hidden_rule is revealed!
        rev_res = client.get(f"/api/play/{sim_token}/reveal")
        assert rev_res.status_code == 200
        rev_data = rev_res.json()
        assert rev_data["config"]["hidden_rule"] == "Barcha savollar Nyuton mexanikasi bo'yicha"
        print("[OK] Preview simulation and reveal behavior verified.")

        # Step 7: Publishing & Idempotency
        print("\n[Step 7] Publishing draft quiz...")
        pub_res = client.post(f"/api/drafts/{draft_id}/publish")
        assert pub_res.status_code == 200
        pub_data = pub_res.json()
        assert pub_data["status"] == "published"
        assert pub_data["published_at"] is not None
        assert pub_data["total_rounds"] == 2
        assert pub_data["total_questions"] == 2
        print(f"[OK] Quiz published successfully at {pub_data['published_at']}")

        # Idempotency check: 2nd publish must return 409 Conflict
        pub2_res = client.post(f"/api/drafts/{draft_id}/publish")
        assert pub2_res.status_code == 409
        print("[OK] Publishing idempotency guard verified (409 Conflict on 2nd attempt).")

        # Step 8: Manifest Self-Containment & Immutability Test
        print("\n[Step 8] Testing manifest self-containment & DB decoupling...")
        # Deliberately mutate DB question
        db_q = db.query(Question).filter(Question.id == q1_id).first()
        db_q.text = "CORRUPTED MUTATED TEXT"
        # Delete accepted answers in DB
        db.query(AcceptedAnswer).filter(AcceptedAnswer.question_id == q1_id).delete()
        db.commit()

        # Start public play session
        live_res = client.post(f"/api/play/start/{quiz_id}")
        assert live_res.status_code == 201
        live_data = live_res.json()
        live_token = live_data["session_token"]

        # Prompt must be from frozen manifest!
        q_text = live_data["current_state"]["question"]["text"]
        assert q_text == "Nyutonning birinchi qonuni nima deb ataladi?", f"Unexpected text: {q_text}"
        assert "CORRUPTED" not in q_text

        # Submit answer -> must evaluate against frozen manifest!
        sub_live = client.post(f"/api/play/{live_token}/answer", json={"answer": "Inersiya qonuni"})
        assert sub_live.status_code == 200
        assert sub_live.json()["is_correct"] is True
        print("[OK] Published gameplay is 100% self-contained in published_manifest.")

    finally:
        # Step 9: Clean up DEV database to restore exact baseline
        print("\n[Step 9] Cleaning up DEV DB...")
        db.rollback()
        # Delete attempts and answer records
        db.query(AnswerRecord).delete()
        db.query(SoloAttempt).delete()
        # Delete test quizzes and cascade
        test_quizzes = db.query(Quiz).filter(Quiz.title.like("DEV Smoke Test%")).all()
        for tq in test_quizzes:
            db.delete(tq)
        # Delete draft questions authored in builder
        draft_qs = db.query(Question).filter(Question.status == "draft").all()
        for dq in draft_qs:
            db.delete(dq)
        db.commit()

        # Verify restoration of exact baseline
        final_q = db.query(Question).count()
        final_ready = db.query(Question).filter(Question.status == "ready").count()
        final_nr = db.query(Question).filter(Question.status == "needs_review").count()
        final_ans = db.query(AcceptedAnswer).count()
        final_quizzes = db.query(Quiz).count()
        final_versions = db.query(QuizVersion).count()
        final_rounds = db.query(Round).count()
        final_rqs = db.query(RoundQuestion).count()
        final_attempts = db.query(SoloAttempt).count()
        final_records = db.query(AnswerRecord).count()

        print("\n=== FINAL RECONCILIATION ===")
        print(f"Questions: {final_q} (expected 3284)")
        print(f"  ready: {final_ready} (expected 3259)")
        print(f"  needs_review: {final_nr} (expected 25)")
        print(f"AcceptedAnswers: {final_ans} (expected 3331)")
        print(f"Quizzes: {final_quizzes} (expected 0)")
        print(f"QuizVersions: {final_versions} (expected 0)")
        print(f"Rounds: {final_rounds} (expected 0)")
        print(f"RoundQuestions: {final_rqs} (expected 0)")
        print(f"SoloAttempts: {final_attempts} (expected 0)")
        print(f"AnswerRecords: {final_records} (expected 0)")

        assert final_q == 3284, f"Baseline question count mismatch: {final_q}"
        assert final_ready == 3259, f"Baseline ready count mismatch: {final_ready}"
        assert final_nr == 25, f"Baseline needs_review count mismatch: {final_nr}"
        assert final_ans == 3331, f"Baseline answer count mismatch: {final_ans}"
        assert final_quizzes == 0
        assert final_versions == 0
        assert final_rounds == 0
        assert final_rqs == 0
        assert final_attempts == 0
        assert final_records == 0

        print("\n[SUCCESS] DEV SMOKE TEST COMPLETE: 100% PASSED AND EXACT BASELINE RESTORED!")
        db.close()

if __name__ == "__main__":
    run_dev_smoke_test()
