import os
from dotenv import load_dotenv

from database import SessionLocal
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer


load_dotenv()


def main():
    db = SessionLocal()

    try:
        # ---------------------------------------------------------
        # DB connection info — password is NEVER printed
        # ---------------------------------------------------------
        from urllib.parse import urlparse

        database_url = os.getenv("DATABASE_URL", "")
        parsed = urlparse(database_url)

        print("=== DATABASE CONNECTION ===")
        print("Host:", parsed.hostname)
        print("Database:", parsed.path.lstrip("/"))
        print()

        # ---------------------------------------------------------
        # Import quiz
        # ---------------------------------------------------------
        quiz = (
            db.query(Quiz)
            .filter_by(title="Telegram Imports Database")
            .first()
        )

        if not quiz:
            print("Telegram Imports Database: NOT FOUND")
            return

        print("=== IMPORT QUIZ ===")
        print("Quiz ID:", quiz.id)
        print("Title:", quiz.title)
        print()

        # ---------------------------------------------------------
        # Version
        # ---------------------------------------------------------
        version = (
            db.query(QuizVersion)
            .filter_by(quiz_id=quiz.id)
            .order_by(QuizVersion.version_number.desc())
            .first()
        )

        if not version:
            print("QuizVersion: NOT FOUND")
            return

        print("QuizVersion ID:", version.id)
        print("Version:", version.version_number)
        print("Status:", version.status)
        print()

        # ---------------------------------------------------------
        # Import round
        # ---------------------------------------------------------
        import_round = (
            db.query(Round)
            .filter_by(
                quiz_version_id=version.id,
                round_type="import_buffer",
            )
            .first()
        )

        if not import_round:
            print("Import round: NOT FOUND")
            return

        print("=== IMPORT ROUND ===")
        print("Round ID:", import_round.id)
        print("Round type:", import_round.round_type)

        question_count = (
            db.query(Question)
            .filter_by(round_id=import_round.id)
            .count()
        )

        print("Questions in import buffer:", question_count)
        print()

        # ---------------------------------------------------------
        # Latest imported questions
        # ---------------------------------------------------------
        questions = (
            db.query(Question)
            .filter_by(round_id=import_round.id)
            .order_by(Question.sequence.desc())
            .limit(20)
            .all()
        )

        print("=== LATEST QUESTIONS ===")

        for q in reversed(questions):
            primary = (
                db.query(AcceptedAnswer)
                .filter(
                    AcceptedAnswer.question_id == q.id,
                    AcceptedAnswer.is_primary.is_(True),
                )
                .first()
            )

            print(f"[Question ID {q.id}]")
            print("Sequence:", q.sequence)
            print("Status:", q.status)
            print("Round type:", import_round.round_type)
            print("Points:", q.points)
            print("Text:", q.text[:100].replace("\n", " "))

            if primary:
                print("Primary answer:", primary.answer_text)
            else:
                print("Primary answer: MISSING")

            print("Source meta:", q.source_meta)
            print("-" * 70)

    finally:
        db.close()


if __name__ == "__main__":
    main()