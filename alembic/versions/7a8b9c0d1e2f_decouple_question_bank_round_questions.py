"""decouple_question_bank_round_questions

Revision ID: 7a8b9c0d1e2f
Revises: 4ed9e2bc20cc
Create Date: 2026-09-17 20:46:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7a8b9c0d1e2f'
down_revision: Union[str, Sequence[str], None] = '4ed9e2bc20cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create round_questions table
    op.create_table(
        'round_questions',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('round_id', sa.Integer(), nullable=False),
        sa.Column('question_id', sa.Integer(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('points_override', sa.Integer(), nullable=True),
        sa.Column('config_override', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['round_id'], ['rounds.id'], name='fk_round_questions_round_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['question_id'], ['questions.id'], name='fk_round_questions_question_id', ondelete='RESTRICT'),
        sa.UniqueConstraint('round_id', 'sequence', name='uq_round_sequence'),
        sa.UniqueConstraint('round_id', 'question_id', name='uq_round_question')
    )
    op.create_index('ix_round_questions_round_seq', 'round_questions', ['round_id', 'sequence'])
    op.create_index('ix_round_questions_question_id', 'round_questions', ['question_id'])

    # 2. Add new columns to questions
    op.add_column('questions', sa.Column('options', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True))
    op.add_column('questions', sa.Column('question_type', sa.String(), server_default='text', nullable=False))
    op.add_column('questions', sa.Column('default_points', sa.Integer(), server_default='1', nullable=True))

    # 3. Add new columns to quiz_versions
    op.add_column('quiz_versions', sa.Column('game_mode', sa.String(), server_default='modern_multiround', nullable=False))
    op.add_column('quiz_versions', sa.Column('published_manifest', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True))

    # 4. Add round_question_id and record_metadata to answer_records
    op.add_column('answer_records', sa.Column('round_question_id', sa.Integer(), nullable=True))
    op.add_column('answer_records', sa.Column('record_metadata', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True))
    op.create_foreign_key('fk_answer_records_round_question_id', 'answer_records', 'round_questions', ['round_question_id'], ['id'])

    # 5. Backfill default_points on questions
    op.execute("UPDATE questions SET default_points = points WHERE default_points IS NULL OR default_points = 1")

    # 6. Backfill round_questions from all existing questions having round_id
    op.execute("""
        INSERT INTO round_questions (round_id, question_id, sequence, points_override, created_at)
        SELECT round_id, id, sequence, points, NOW()
        FROM questions
        WHERE round_id IS NOT NULL
        ORDER BY id ASC;
    """)

    # 7. Backfill answer_records.round_question_id
    op.execute("""
        UPDATE answer_records ar
        SET round_question_id = rq.id
        FROM solo_attempts sa, rounds r, round_questions rq
        WHERE ar.attempt_id = sa.id
          AND sa.quiz_version_id = r.quiz_version_id
          AND rq.round_id = r.id
          AND rq.question_id = ar.question_id;
    """)

    # 8. Relax questions.round_id and questions.sequence nullability
    op.alter_column('questions', 'round_id', existing_type=sa.Integer(), nullable=True)
    op.alter_column('questions', 'sequence', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # 1. Restore questions nullability (only if non-null in practice)
    op.alter_column('questions', 'sequence', existing_type=sa.Integer(), nullable=False)
    op.alter_column('questions', 'round_id', existing_type=sa.Integer(), nullable=False)

    # 2. Drop constraints & columns from answer_records
    op.drop_constraint('fk_answer_records_round_question_id', 'answer_records', type_='foreignkey')
    op.drop_column('answer_records', 'record_metadata')
    op.drop_column('answer_records', 'round_question_id')

    # 3. Drop columns from quiz_versions
    op.drop_column('quiz_versions', 'published_manifest')
    op.drop_column('quiz_versions', 'game_mode')

    # 4. Drop columns from questions
    op.drop_column('questions', 'default_points')
    op.drop_column('questions', 'question_type')
    op.drop_column('questions', 'options')

    # 5. Drop round_questions indexes and table
    op.drop_index('ix_round_questions_question_id', table_name='round_questions')
    op.drop_index('ix_round_questions_round_seq', table_name='round_questions')
    op.drop_table('round_questions')
