from database import SessionLocal
from models import Quiz, QuizVersion, Round, Question, AcceptedAnswer

# Уникальное имя для безопасного поиска и удаления
DEMO_TITLE = "[DEMO] ZakoWhat Foundation"

def run_seed():
    db = SessionLocal()
    
    # 1. Безопасный поиск: ищем ТОЛЬКО наш демо-квиз
    existing_demo = db.query(Quiz).filter(Quiz.title == DEMO_TITLE).first()
    
    if existing_demo:
        print(f"Найден старый демо-квиз '{DEMO_TITLE}'. Удаляем его для обновления...")
        db.delete(existing_demo)
        db.commit()

    print("Заполняем базу данных (Seeding)...")

    # 2. Создаем демо-квиз с тем же уникальным именем
    quiz = Quiz(title=DEMO_TITLE, description="Тестовый набор данных для проверки механик.")
    db.add(quiz)
    db.commit()

    # Создаем версию
    version = QuizVersion(quiz_id=quiz.id, version_number=1)
    db.add(version)
    db.commit()

    # --- Дальше код остается без изменений ---
    
    # Раунд 1: Стандартный
    r1 = Round(quiz_version_id=version.id, sequence=1, round_type="standard")
    db.add(r1)
    db.commit()
    q1 = Question(round_id=r1.id, sequence=1, text="Какой город является столицей Узбекистана?", points=1)
    db.add(q1)
    db.commit()
    db.add_all([
        AcceptedAnswer(question_id=q1.id, answer_text="Tashkent"),
        AcceptedAnswer(question_id=q1.id, answer_text="Ташкент")
    ])

    # Раунд 2: True/False (Aldama meni)
    r2 = Round(quiz_version_id=version.id, sequence=2, round_type="true_false")
    db.add(r2)
    db.commit()
    q2 = Question(round_id=r2.id, sequence=1, text="Тихий океан - самый большой на Земле.", points=1)
    db.add(q2)
    db.commit()
    db.add(AcceptedAnswer(question_id=q2.id, answer_text="true"))

    # Раунд 3: Mantiqasqon
    r3 = Round(quiz_version_id=version.id, sequence=3, round_type="mantiqasqon", config={"hidden_rule": "Все ответы - это цвета"})
    db.add(r3)
    db.commit()
    q3 = Question(round_id=r3.id, sequence=1, text="Оптическое явление, которое мы видим, глядя на дневное безоблачное небо.", points=2)
    q4 = Question(round_id=r3.id, sequence=2, text="Цвет, который ассоциируется с марсом и пожарной машиной.", points=2)
    db.add_all([q3, q4])
    db.commit()
    db.add(AcceptedAnswer(question_id=q3.id, answer_text="синий"))
    db.add(AcceptedAnswer(question_id=q4.id, answer_text="красный"))

    # Раунд 4: Zanjir
    r4 = Round(quiz_version_id=version.id, sequence=4, round_type="zanjir")
    db.add(r4)
    db.commit()
    q5 = Question(round_id=r4.id, sequence=1, text="Крупное домашнее или дикое животное семейства кошачьих (3 буквы)", points=1)
    db.add(q5)
    db.commit()
    db.add(AcceptedAnswer(question_id=q5.id, answer_text="КОТ"))
    
    q6 = Question(round_id=r4.id, sequence=2, text="Синоним слова 'мрак', отсутствие света (4 буквы)", points=1)
    db.add(q6)
    db.commit()
    db.add(AcceptedAnswer(question_id=q6.id, answer_text="ТЬМА"))

    db.commit()
    db.close()
    print("Посев завершен! Добавлено: 1 Quiz, 1 Version, 4 Rounds, и 6 Questions.")

if __name__ == "__main__":
    run_seed()