from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, JSON
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base

class Quiz(Base):
    """The editable parent container for a quiz."""
    __tablename__ = "quizzes"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    # creator_id will be added later when we build authentication
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    versions = relationship("QuizVersion", back_populates="quiz", cascade="all, delete-orphan")

class QuizVersion(Base):
    """The immutable published snapshot of a quiz."""
    __tablename__ = "quiz_versions"
    
    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    published_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    quiz = relationship("Quiz", back_populates="versions")
    rounds = relationship("Round", back_populates="quiz_version", cascade="all, delete-orphan")

class Round(Base):
    """A specific game mechanic block within a quiz version."""
    __tablename__ = "rounds"
    
    id = Column(Integer, primary_key=True, index=True)
    quiz_version_id = Column(Integer, ForeignKey("quiz_versions.id"), nullable=False)
    sequence = Column(Integer, nullable=False)
    round_type = Column(String, nullable=False) # e.g., standard, true_false, zanjir, mantiqasqon, music
    config = Column(JSON, nullable=True) # Lightweight config (e.g., {"hidden_rule": "All answers are colors"})
    
    quiz_version = relationship("QuizVersion", back_populates="rounds")
    questions = relationship("Question", back_populates="round", cascade="all, delete-orphan")

class Question(Base):
    """An individual question within a round."""
    __tablename__ = "questions"
    
    id = Column(Integer, primary_key=True, index=True)
    round_id = Column(Integer, ForeignKey("rounds.id"), nullable=False)
    sequence = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    media_provider = Column(String, nullable=True) # e.g., "youtube"
    media_url = Column(String, nullable=True)
    points = Column(Integer, default=1)
    
    round = relationship("Round", back_populates="questions")
    accepted_answers = relationship("AcceptedAnswer", back_populates="question", cascade="all, delete-orphan")

class AcceptedAnswer(Base):
    """The expected correct answers for a question. 
    Standard rounds may have multiple. Zanjir will have exactly one."""
    __tablename__ = "accepted_answers"
    
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    answer_text = Column(String, nullable=False)
    
    question = relationship("Question", back_populates="accepted_answers")