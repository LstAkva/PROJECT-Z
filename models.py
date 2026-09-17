import secrets
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, JSON, Boolean, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from database import Base

# --- КВИЗ И ВЕРСИОНИРОВАНИЕ ---

class Quiz(Base):
    """Редактируемый родительский контейнер для квиза / пакета."""
    __tablename__ = "quizzes"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    versions = relationship("QuizVersion", back_populates="quiz", cascade="all, delete-orphan")

class QuizVersion(Base):
    """Версия квиза (draft, published, archived) с неизменяемым published_manifest."""
    __tablename__ = "quiz_versions"
    
    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    status = Column(String, default="draft", nullable=False)  # draft, published, archived
    game_mode = Column(String, default="modern_multiround", nullable=False)  # modern_multiround, classic_zakovat, svoyak
    published_manifest = Column(JSONB().with_variant(JSON, "sqlite"), nullable=True)
    published_at = Column(DateTime, nullable=True)
    
    quiz = relationship("Quiz", back_populates="versions")
    rounds = relationship("Round", back_populates="quiz_version", cascade="all, delete-orphan", order_by="Round.sequence")
    solo_attempts = relationship("SoloAttempt", back_populates="quiz_version", cascade="all, delete-orphan")

class Round(Base):
    """Специфичный блок игровой механики / тур внутри версии квиза."""
    __tablename__ = "rounds"
    
    id = Column(Integer, primary_key=True, index=True)
    quiz_version_id = Column(Integer, ForeignKey("quiz_versions.id"), nullable=False)
    sequence = Column(Integer, nullable=False)
    round_type = Column(String, nullable=False)
    config = Column(JSON, nullable=True)
    
    quiz_version = relationship("QuizVersion", back_populates="rounds")
    questions = relationship("Question", back_populates="round", cascade="all, delete-orphan")  # legacy backward-compat
    round_questions = relationship(
        "RoundQuestion",
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="RoundQuestion.sequence"
    )

class Question(Base):
    """Канонический переиспользуемый вопрос в общем Банке Вопросов (Question Bank)."""
    __tablename__ = "questions"
    
    id = Column(Integer, primary_key=True, index=True)
    round_id = Column(Integer, ForeignKey("rounds.id"), nullable=True)  # legacy backward-compat
    sequence = Column(Integer, nullable=True)                           # legacy backward-compat
    text = Column(Text, nullable=False)
    media_provider = Column(String, nullable=True)
    media_url = Column(String, nullable=True)
    points = Column(Integer, default=1)
    default_points = Column(Integer, default=1, nullable=True)
    
    explanation = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="draft", server_default="draft")
    source_meta = Column(JSONB().with_variant(JSON, "sqlite"), nullable=True)
    compound_group_id = Column(String, nullable=True)
    compound_type = Column(String, nullable=True)
    category = Column(String, nullable=True)
    options = Column(JSONB().with_variant(JSON, "sqlite"), nullable=True)
    question_type = Column(String, nullable=False, default="text", server_default="text")

    round = relationship("Round", back_populates="questions")  # legacy backward-compat
    round_questions = relationship("RoundQuestion", back_populates="question", cascade="all, delete-orphan")
    accepted_answers = relationship("AcceptedAnswer", back_populates="question", cascade="all, delete-orphan")

class RoundQuestion(Base):
    """Размещение вопроса из Question Bank в конкретном раунде с порядковым номером и оверрайдами."""
    __tablename__ = "round_questions"
    __table_args__ = (
        UniqueConstraint("round_id", "sequence", name="uq_round_sequence"),
        UniqueConstraint("round_id", "question_id", name="uq_round_question"),
    )

    id = Column(Integer, primary_key=True, index=True)
    round_id = Column(Integer, ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False)
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    points_override = Column(Integer, nullable=True)
    config_override = Column(JSONB().with_variant(JSON, "sqlite"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    round = relationship("Round", back_populates="round_questions")
    question = relationship("Question", back_populates="round_questions")
    answer_records = relationship("AnswerRecord", back_populates="round_question")

class AcceptedAnswer(Base):
    """Ожидаемые правильные ответы для вопроса."""
    __tablename__ = "accepted_answers"
    
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    answer_text = Column(String, nullable=False)
    is_primary = Column(Boolean, nullable=False, default=False, server_default="false")
    
    question = relationship("Question", back_populates="accepted_answers")

# --- ИГРОВАЯ СЕССИЯ И ОТВЕТЫ ИГРОКА ---

class SoloAttempt(Base):
    """Отдельная игра конкретного игрока."""
    __tablename__ = "solo_attempts"

    id = Column(Integer, primary_key=True, index=True)
    quiz_version_id = Column(Integer, ForeignKey("quiz_versions.id"), nullable=False)
    user_id = Column(Integer, nullable=True, index=True)
    session_token = Column(String(64), unique=True, index=True, nullable=False, default=lambda: secrets.token_urlsafe(32))
    timer_mode = Column(String, default="standard", nullable=False)
    status = Column(String, default="in_progress", nullable=False)  # in_progress, round_reveal, completed
    current_round_index = Column(Integer, default=0, nullable=False)
    current_question_index = Column(Integer, default=0, nullable=False)
    total_score = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    quiz_version = relationship("QuizVersion", back_populates="solo_attempts")
    answers = relationship("AnswerRecord", back_populates="attempt", cascade="all, delete-orphan")

class AnswerRecord(Base):
    """Запись каждого ответа, который ввел игрок."""
    __tablename__ = "answer_records"

    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(Integer, ForeignKey("solo_attempts.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    round_question_id = Column(Integer, ForeignKey("round_questions.id"), nullable=True)
    submitted_text = Column(String, nullable=False)
    is_correct = Column(Boolean, nullable=False)
    points_awarded = Column(Integer, default=0, nullable=False)
    answered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    record_metadata = Column(JSONB().with_variant(JSON, "sqlite"), nullable=True)

    attempt = relationship("SoloAttempt", back_populates="answers")
    question = relationship("Question")
    round_question = relationship("RoundQuestion", back_populates="answer_records")