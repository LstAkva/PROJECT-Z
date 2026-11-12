"""
Local-only QA server for real gameplay playthroughs.

Runs the unmodified application against a THROWAWAY SQLite file inside scratch/ (git-ignored), so
nothing touches the development or production databases and no test account outlives the file.
Authentication is not bypassed or weakened: the QA player registers and signs in through the
application's own /api/auth endpoints.

    venv/Scripts/python.exe scripts/qa_server.py            # http://127.0.0.1:8011

Delete scratch/qa_playthrough.db to start from a clean slate. See docs/GAMEPLAY_AND_FRONTEND.md.
"""
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "qa_playthrough.db"

# Must be set BEFORE the app modules load (.env values never override an existing variable).
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_TARGET"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}?check_same_thread=false"
# QA-only signing key, generated once and kept beside the throwaway database (scratch/ is git-ignored),
# so a restart of this server keeps the QA player signed in. Never the real key.
_KEY_FILE = SCRATCH / "qa_secret.key"
if not _KEY_FILE.exists():
    _KEY_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
os.environ["SECRET_KEY"] = _KEY_FILE.read_text(encoding="utf-8").strip()
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["OWNER_EMAIL"] = ""
os.environ["OWNER_EMAILS"] = ""

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from database import Base, SessionLocal, engine  # noqa: E402
import models  # noqa: E402,F401
from models import AcceptedAnswer, Question, Quiz, QuizVersion, Round, RoundQuestion  # noqa: E402

assert str(engine.url).startswith("sqlite"), "QA server must only ever use the throwaway SQLite file"

LONG_QUESTION = (
    "XIX asr oxirida Turkistonga kelgan sayyohlardan biri o‘z kundaligida shunday yozadi: «Bu shaharda har bir "
    "hovlining o‘z suvi, har bir mahallaning o‘z ustasi bor; ammo eng hayratlanarlisi — bozordagi savdogarlar "
    "hisob-kitobni hech qanday qog‘ozsiz, faqat xotira va barmoqlar yordamida yuritishadi». Savol: sayyoh qaysi "
    "shahar haqida yozgan?"
)


def _add_question(db, round_obj, seq, text, answer, points=1, explanation=None, media_url=None):
    q = Question(text=text, points=points, default_points=points, status="approved",
                 explanation=explanation, media_url=media_url)
    db.add(q)
    db.flush()
    a = AcceptedAnswer(question_id=q.id, answer_text=answer, is_primary=True)
    db.add(a)
    rq = RoundQuestion(round_id=round_obj.id, question_id=q.id, sequence=seq, points_override=points)
    db.add(rq)
    db.flush()
    item = {"question_id": q.id, "round_question_id": rq.id, "sequence": seq, "text": text, "points": points,
            "primary_answer": answer, "accepted_answers": [{"answer_text": answer, "is_primary": True}]}
    if explanation:
        item["explanation"] = explanation
    if media_url:
        item["media_url"] = media_url
    return item


def seed_classic(db):
    quiz = Quiz(title="Classic Zakovat — QA to‘plami", description="Mahalliy sinov uchun 2×12 to‘plam.")
    db.add(quiz)
    db.flush()
    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="classic_zakovat")
    db.add(version)
    db.flush()
    rounds = []
    for r_seq in (1, 2):
        # Round 1 uses a short explicit limit so a timeout can be observed without a two-minute wait.
        config = {"name": f"{r_seq}-tur"}
        if r_seq == 1:
            config["time_limit_seconds"] = 20
        round_obj = Round(quiz_version_id=version.id, sequence=r_seq, round_type="zakovat_classic", config=config)
        db.add(round_obj)
        db.flush()
        questions = []
        for q_seq in range(1, 13):
            n = (r_seq - 1) * 12 + q_seq
            text = f"{n}-savol: sinov savoli matni. To‘g‘ri javob — «javob {n}»."
            explanation = f"{n}-savol uchun izoh: bu yerda qisqa tushuntirish beriladi." if n % 3 == 0 else None
            media = None
            if n == 2:
                text = LONG_QUESTION + " (To‘g‘ri javob — «javob 2».)"
            if n == 3:
                media = "/static/svg/zakowhat-app-icon.svg"
            questions.append(_add_question(db, round_obj, q_seq, text, f"javob {n}", 1, explanation, media))
        rounds.append({"round_id": round_obj.id, "sequence": r_seq, "round_type": "zakovat_classic",
                       "config": config, "questions": questions})
    version.published_manifest = {"title": quiz.title, "game_mode": "classic_zakovat", "category": "Tarix",
                                  "visibility": "public", "rounds": rounds}
    return quiz


def seed_svoyak(db):
    quiz = Quiz(title="Svoya Igra — QA to‘plami", description="Mahalliy sinov uchun 5×5 to‘plam.")
    db.add(quiz)
    db.flush()
    version = QuizVersion(quiz_id=quiz.id, version_number=1, status="published", game_mode="svoyak")
    db.add(version)
    db.flush()
    themes = ["Jahon tarixi va hukmdorlar", "Sayyora geografiyasi", "Madaniyat, sanʼat va sport",
              "So‘zlar va sonlar", "O‘zbekiston bo‘ylab"]
    rounds = []
    for r_seq, theme in enumerate(themes, start=1):
        config = {"theme_title": theme}
        round_obj = Round(quiz_version_id=version.id, sequence=r_seq, round_type="svoyak_theme", config=config)
        db.add(round_obj)
        db.flush()
        questions = []
        for q_seq, pts in enumerate((10, 20, 30, 40, 50), start=1):
            text = f"{theme}: {pts} ballik sinov savoli. To‘g‘ri javob — «javob {r_seq}-{pts}»."
            questions.append(_add_question(db, round_obj, q_seq, text, f"javob {r_seq}-{pts}", pts,
                                           f"{theme}, {pts} ball: izoh matni."))
        rounds.append({"round_id": round_obj.id, "sequence": r_seq, "round_type": "svoyak_theme",
                       "config": config, "questions": questions})
    version.published_manifest = {"title": quiz.title, "game_mode": "svoyak", "category": "Mantiq",
                                  "visibility": "public", "rounds": rounds}
    return quiz


def ensure_seed():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.query(Quiz).count() == 0:
            seed_classic(db)
            seed_svoyak(db)
            db.commit()
            print("QA database seeded: 1 Classic Zakovat pack, 1 Svoya Igra pack")
        else:
            print("QA database already seeded")
    finally:
        db.close()


if __name__ == "__main__":
    ensure_seed()
    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=8011, log_level="warning")
