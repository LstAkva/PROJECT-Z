"""add_users_and_attempt_leaderboard_fields

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-09-18 19:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8b9c0d1e2f3a'
down_revision: Union[str, Sequence[str], None] = '7a8b9c0d1e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('hashed_password', sa.String(length=255), nullable=True),
        sa.Column('display_name', sa.String(length=100), nullable=False),
        sa.Column('auth_provider', sa.String(length=50), server_default='local', nullable=False),
        sa.Column('google_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_users_id', 'users', ['id'], unique=False)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    op.create_index('ix_users_google_id', 'users', ['google_id'], unique=True)

    # 2. Add columns to solo_attempts
    op.add_column('solo_attempts', sa.Column('anon_id', sa.String(length=64), nullable=True))
    op.add_column('solo_attempts', sa.Column('total_correct', sa.Integer(), server_default='0', nullable=False))
    op.add_column('solo_attempts', sa.Column('active_time_seconds', sa.Integer(), server_default='0', nullable=False))
    op.add_column('solo_attempts', sa.Column('question_opened_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_solo_attempts_anon_id', 'solo_attempts', ['anon_id'], unique=False)

    # 3. Add FK from solo_attempts.user_id to users.id
    op.create_foreign_key('fk_solo_attempts_user_id', 'solo_attempts', 'users', ['user_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_solo_attempts_user_id', 'solo_attempts', type_='foreignkey')
    op.drop_index('ix_solo_attempts_anon_id', table_name='solo_attempts')
    op.drop_column('solo_attempts', 'question_opened_at')
    op.drop_column('solo_attempts', 'active_time_seconds')
    op.drop_column('solo_attempts', 'total_correct')
    op.drop_column('solo_attempts', 'anon_id')
    op.drop_index('ix_users_google_id', table_name='users')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_index('ix_users_id', table_name='users')
    op.drop_table('users')
