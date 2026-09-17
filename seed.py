from database import SessionLocal
from models import Quiz, QuizVersion, Round, Question, RoundQuestion, AcceptedAnswer
from api.quizzes import compile_published_manifest

DEMO_TITLE = "ZakoWhat Milliy Chempionati (Namuna)"

def run_seed():
    db = SessionLocal()
    
    # 1. Безопасный поиск: ищем ТОЛЬКО наш демо-квиз
    existing_demo = db.query(Quiz).filter(Quiz.title == DEMO_TITLE).first()
    
    if existing_demo:
        print(f"Eski demo-kviz '{DEMO_TITLE}' topildi. Yangilash uchun o'chirilmoqda...")
        db.delete(existing_demo)
        db.commit()

    print("Bazaga namunaviy o'zbek tili ma'lumotlarini kiritish (Seeding)...")

    # 2. Создаем демо-квиз с тем же уникальным именем
    quiz = Quiz(
        title=DEMO_TITLE,
        description="O'zbek intellektual viktorinasi: Standart, To'g'ri/Noto'g'ri, Mantiqasqon va Zanjir raundlari."
    )
    db.add(quiz)
    db.commit()

    # Создаем опубликованную версию
    version = QuizVersion(
        quiz_id=quiz.id,
        version_number=1,
        status="published",
        game_mode="modern_multiround"
    )
    db.add(version)
    db.commit()

    # Raund 1: Standart
    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db.add(r1)
    db.commit()
    q1 = Question(text="O'zbekiston Respublikasining poytaxti qaysi shahar?", points=1, default_points=1, status="ready")
    db.add(q1)
    db.commit()
    db.add(RoundQuestion(round_id=r1.id, question_id=q1.id, sequence=1, points_override=1))
    db.add_all([
        AcceptedAnswer(question_id=q1.id, answer_text="Toshkent", is_primary=True),
        AcceptedAnswer(question_id=q1.id, answer_text="Toshkent shahri", is_primary=False)
    ])
    db.commit()

    # Raund 2: True/False (To'g'ri / Noto'g'ri)
    r2 = Round(quiz_version_id=version.id, sequence=2, round_type="true_false")
    db.add(r2)
    db.commit()
    q2 = Question(text="Yer sayyorasi Quyosh atrofida aylanadi.", points=1, default_points=1, status="ready", question_type="true_false")
    db.add(q2)
    db.commit()
    db.add(RoundQuestion(round_id=r2.id, question_id=q2.id, sequence=1, points_override=1))
    db.add(AcceptedAnswer(question_id=q2.id, answer_text="rost", is_primary=True))
    db.commit()

    # Raund 3: Mantiqasqon
    r3 = Round(
        quiz_version_id=version.id,
        sequence=3,
        round_type="mantiqasqon",
        config={"hidden_rule": "Barcha javoblar mevalar nomlari"}
    )
    db.add(r3)
    db.commit()
    q3 = Question(text="Daraxtda o'sadigan shirin, dumaloq, xalq ertaklarida sehrli hisoblangan meva.", points=2, default_points=2, status="ready")
    q4 = Question(text="Choyga qo'shib ichiladigan nordon sariq sitrus mevasi.", points=2, default_points=2, status="ready")
    db.add_all([q3, q4])
    db.commit()
    db.add(RoundQuestion(round_id=r3.id, question_id=q3.id, sequence=1, points_override=2))
    db.add(RoundQuestion(round_id=r3.id, question_id=q4.id, sequence=2, points_override=2))
    db.add(AcceptedAnswer(question_id=q3.id, answer_text="olma", is_primary=True))
    db.add(AcceptedAnswer(question_id=q4.id, answer_text="limon", is_primary=True))
    db.commit()

    # Raund 4: Zanjir
    r4 = Round(quiz_version_id=version.id, sequence=4, round_type="zanjir")
    db.add(r4)
    db.commit()
    # Zanjir: quyosh (sh) -> shahar (r) -> rishton (n)
    q5 = Question(text="Kunduz kuni osmonda charog'on nur sochib turuvchi ulkan yulduz.", points=1, default_points=1, status="ready")
    q6 = Question(text="Aholisi qishloq xo'jaligidan boshqa sohalarda band bo'lgan yirik ma'muriy markaz.", points=1, default_points=1, status="ready")
    q7 = Question(text="Farg'ona vodiysida joylashgan qadimiy kulolchilik va hunarmandchilik maskani.", points=1, default_points=1, status="ready")
    db.add_all([q5, q6, q7])
    db.commit()
    db.add(RoundQuestion(round_id=r4.id, question_id=q5.id, sequence=1, points_override=1))
    db.add(RoundQuestion(round_id=r4.id, question_id=q6.id, sequence=2, points_override=1))
    db.add(RoundQuestion(round_id=r4.id, question_id=q7.id, sequence=3, points_override=1))
    db.add(AcceptedAnswer(question_id=q5.id, answer_text="quyosh", is_primary=True))
    db.add(AcceptedAnswer(question_id=q6.id, answer_text="shahar", is_primary=True))
    db.add(AcceptedAnswer(question_id=q7.id, answer_text="rishton", is_primary=True))
    db.commit()

    # Compile immutable published_manifest
    version.published_manifest = compile_published_manifest(version, db)
    db.commit()
    db.close()
    print("Muvaffaqiyatli yakunlandi! 1 ta Quiz, 1 ta Versiya, 4 ta Raund, 7 ta Savol va 7 ta RoundQuestion kiritildi.")

if __name__ == "__main__":
    run_seed()