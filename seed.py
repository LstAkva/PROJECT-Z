from database import SessionLocal
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer

def run_seed():
    db = SessionLocal()
    
    # 1. Clear existing seed data to prevent duplicates if run multiple times
    db.query(Quiz).delete()
    db.commit()

    print("Seeding database...")

    # Create Parent Quiz
    quiz = Quiz(title="ZakoWhat Demo: The Foundation", description="A tiny realistic dataset to test mechanics.")
    db.add(quiz)
    db.commit()

    # Create Immutable Version
    version = QuizVersion(quiz_id=quiz.id, version_number=1)
    db.add(version)
    db.commit()

    # Round 1: Standard
    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db.add(r1)
    db.commit()
    q1 = Question(round_id=r1.id, sequence=1, text="What is the capital of Uzbekistan?", points=1)
    db.add(q1)
    db.commit()
    db.add_all([
        AcceptedAnswer(question_id=q1.id, answer_text="Tashkent"),
        AcceptedAnswer(question_id=q1.id, answer_text="Ташкент") # Multiple accepted variants
    ])

    # Round 2: True/False (Aldama meni)
    r2 = Round(quiz_version_id=version.id, sequence=2, round_type="true_false")
    db.add(r2)
    db.commit()
    q2 = Question(round_id=r2.id, sequence=1, text="The Pacific Ocean is the largest ocean on Earth.", points=1)
    db.add(q2)
    db.commit()
    db.add(AcceptedAnswer(question_id=q2.id, answer_text="true"))

    # Round 3: Mantiqasqon
    r3 = Round(quiz_version_id=version.id, sequence=3, round_type="mantiqasqon", config={"hidden_rule": "All answers are colors"})
    db.add(r3)
    db.commit()
    q3 = Question(round_id=r3.id, sequence=1, text="The visual appearance of a clear daytime sky.", points=2)
    q4 = Question(round_id=r3.id, sequence=2, text="The color that rhymes with 'bed'.", points=2)
    db.add_all([q3, q4])
    db.commit()
    db.add(AcceptedAnswer(question_id=q3.id, answer_text="blue"))
    db.add(AcceptedAnswer(question_id=q4.id, answer_text="red"))

    # Round 4: Zanjir
    r4 = Round(quiz_version_id=version.id, sequence=4, round_type="zanjir")
    db.add(r4)
    db.commit()
    q5 = Question(round_id=r4.id, sequence=1, text="A large domesticated feline", points=1)
    db.add(q5)
    db.commit()
    db.add(AcceptedAnswer(question_id=q5.id, answer_text="TIGER")) # Ends in R
    
    q6 = Question(round_id=r4.id, sequence=2, text="A rodent often chased by a cat", points=1)
    db.add(q6)
    db.commit()
    db.add(AcceptedAnswer(question_id=q6.id, answer_text="RAT")) # Starts with R, ends in T

    db.commit()
    db.close()
    print("Seed complete! Added 1 Quiz, 1 Version, 4 Rounds, and 6 Questions.")

if __name__ == "__main__":
    run_seed()