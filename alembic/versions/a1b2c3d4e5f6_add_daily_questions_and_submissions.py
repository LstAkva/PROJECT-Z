"""add_daily_questions_and_submissions

Revision ID: a1b2c3d4e5f6
Revises: 9c0d1e2f3a4b
Create Date: 2026-10-28 21:20:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '9c0d1e2f3a4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # 1. Create daily_questions table if not exists
    if 'daily_questions' not in tables:
        op.create_table(
            'daily_questions',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('schedule_date', sa.Date(), nullable=False),
            sa.Column('question_id', sa.String(length=100), nullable=False),
            sa.Column('message_id', sa.Integer(), nullable=False),
            sa.Column('channel_id', sa.BigInteger(), nullable=False, server_default='2010763825'),
            sa.Column('question_text', sa.Text(), nullable=False),
            sa.Column('primary_answer', sa.String(length=500), nullable=False),
            sa.Column('explanation', sa.Text(), nullable=True),
            sa.Column(
                'accepted_answers',
                postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'),
                nullable=False,
            ),
            sa.Column(
                'source_meta',
                postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'),
                nullable=True,
            ),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_daily_questions_schedule_date', 'daily_questions', ['schedule_date'], unique=True)
        op.create_index('ix_daily_questions_message_id', 'daily_questions', ['message_id'])

    # 2. Create daily_submissions table if not exists
    if 'daily_submissions' not in tables:
        op.create_table(
            'daily_submissions',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('daily_question_id', sa.Integer(), sa.ForeignKey('daily_questions.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
            sa.Column('anon_id', sa.String(length=64), nullable=True),
            sa.Column('submitted_text', sa.String(length=500), nullable=False),
            sa.Column('is_correct', sa.Boolean(), nullable=False),
            sa.Column('answered_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column(
                'submission_meta',
                postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'),
                nullable=True,
            ),
            sa.UniqueConstraint('daily_question_id', 'user_id', name='uq_daily_submission_question_user'),
        )
        op.create_index('ix_daily_submissions_daily_question_id', 'daily_submissions', ['daily_question_id'])
        op.create_index('ix_daily_submissions_user_id', 'daily_submissions', ['user_id'])
        op.create_index('ix_daily_submissions_anon_id', 'daily_submissions', ['anon_id'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'daily_submissions' in tables:
        op.drop_table('daily_submissions')

    if 'daily_questions' in tables:
        op.drop_table('daily_questions')
