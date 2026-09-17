import secrets
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, JSON, Boolean
from sqlalchemy.dialects.postgresql import JSONB  # <-- ДОБАВЛЕН ИМПОРТ
from sqlalchemy.orm import relationship
from database import Base

# --- СТАРЫЕ МОДЕЛИ (КОНТЕНТ КВИЗА) ---

class Quiz(Base):
    """Редактируемый родительский контейнер для квиза."""
    __tablename__ = "quizzes"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    versions = relationship("QuizVersion", back_populates="quiz", cascade="all, delete-orphan")

class QuizVersion(Base):
    """Неизменяемый опубликованный слепок (snapshot) квиза."""
    __tablename__ = "quiz_versions"
    
    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    status = Column(String, default="draft", nullable=False) # draft, published, archived
    published_at = Column(DateTime, nullable=True) 
    
    quiz = relationship("Quiz", back_populates="versions")
    rounds = relationship("Round", back_populates="quiz_version", cascade="all, delete-orphan")

class Round(Base):
    """Специфичный блок игровой механики внутри версии квиза."""
    __tablename__ = "rounds"
    
    id = Column(Integer, primary_key=True, index=True)
    quiz_version_id = Column(Integer, ForeignKey("quiz_versions.id"), nullable=False)
    sequence = Column(Integer, nullable=False)
    round_type = Column(String, nullable=False)
    config = Column(JSON, nullable=True)
    
    quiz_version = relationship("QuizVersion", back_populates="rounds")
    questions = relationship("Question", back_populates="round", cascade="all, delete-orphan")

class Question(Base):
    """Отдельный вопрос внутри раунда."""
    __tablename__ = "questions"
    
    id = Column(Integer, primary_key=True, index=True)
    round_id = Column(Integer, ForeignKey("rounds.id"), nullable=False)
    sequence = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    media_provider = Column(String, nullable=True)
    media_url = Column(String, nullable=True)
    points = Column(Integer, default=1)
    
    # --- НОВЫЕ ПОЛЯ (Import Contract v1) ---
    explanation = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="draft", server_default="draft")
    source_meta = Column(JSONB, nullable=True)
    compound_group_id = Column(String, nullable=True)
    compound_type = Column(String, nullable=True)
    category = Column(String, nullable=True)
    # ---------------------------------------

    round = relationship("Round", back_populates="questions")
    accepted_answers = relationship("AcceptedAnswer", back_populates="question", cascade="all, delete-orphan")

class AcceptedAnswer(Base):
    """Ожидаемые правильные ответы для вопроса."""
    __tablename__ = "accepted_answers"
    
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    answer_text = Column(String, nullable=False)
    
    # --- НОВОЕ ПОЛЕ (Import Contract v1) ---
    is_primary = Column(Boolean, nullable=False, default=False, server_default="false")
    # ---------------------------------------
    
    question = relationship("Question", back_populates="accepted_answers")


# --- НОВЫЕ МОДЕЛИ (ИГРОВАЯ СЕССИЯ И ОТВЕТЫ ИГРОКА) ---

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

    quiz_version = relationship("QuizVersion")
    answers = relationship("AnswerRecord", back_populates="attempt", cascade="all, delete-orphan")


class AnswerRecord(Base):
    """Запись каждого ответа, который ввел игрок."""
    __tablename__ = "answer_records"

    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(Integer, ForeignKey("solo_attempts.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    submitted_text = Column(String, nullable=False)
    is_correct = Column(Boolean, nullable=False)
    points_awarded = Column(Integer, default=0, nullable=False)
    answered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    attempt = relationship("SoloAttempt", back_populates="answers")
    question = relationship("Question")