"""add_live_rooms

Live rooms (host-led games joined with a code): live_rooms, live_teams, live_answers.
New tables only; nothing existing changes.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-26 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if 'live_rooms' not in tables:
        op.create_table(
            'live_rooms',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('code', sa.String(length=8), nullable=False),
            sa.Column('host_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('quiz_version_id', sa.Integer(), sa.ForeignKey('quiz_versions.id'), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='lobby'),
            sa.Column('current_index', sa.Integer(), nullable=False, server_default='-1'),
            sa.Column('seconds_per_question', sa.Integer(), nullable=False, server_default='60'),
            sa.Column('question_deadline', sa.DateTime(), nullable=True),
            sa.Column('paused_remaining', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_live_rooms_id', 'live_rooms', ['id'])
        op.create_index('ix_live_rooms_code', 'live_rooms', ['code'], unique=True)
        op.create_index('ix_live_rooms_host_user_id', 'live_rooms', ['host_user_id'])
        op.create_index('ix_live_rooms_created_at', 'live_rooms', ['created_at'])
    if 'live_teams' not in tables:
        op.create_table(
            'live_teams',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('room_id', sa.Integer(), sa.ForeignKey('live_rooms.id', ondelete='CASCADE'), nullable=False),
            sa.Column('name', sa.String(length=40), nullable=False),
            sa.Column('name_key', sa.String(length=40), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('removed', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('joined_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('room_id', 'name_key', name='uq_live_team_name'),
            sa.UniqueConstraint('token_hash', name='uq_live_team_token_hash'),
        )
        op.create_index('ix_live_teams_id', 'live_teams', ['id'])
        op.create_index('ix_live_teams_room_id', 'live_teams', ['room_id'])
    if 'live_answers' not in tables:
        op.create_table(
            'live_answers',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('room_id', sa.Integer(), sa.ForeignKey('live_rooms.id', ondelete='CASCADE'), nullable=False),
            sa.Column('team_id', sa.Integer(), sa.ForeignKey('live_teams.id', ondelete='CASCADE'), nullable=False),
            sa.Column('question_index', sa.Integer(), nullable=False),
            sa.Column('submitted_text', sa.String(length=300), nullable=False),
            sa.Column('auto_correct', sa.Boolean(), nullable=False),
            sa.Column('host_verdict', sa.Boolean(), nullable=True),
            sa.Column('answered_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('team_id', 'question_index', name='uq_live_answer_team_question'),
        )
        op.create_index('ix_live_answers_id', 'live_answers', ['id'])
        op.create_index('ix_live_answers_room_id', 'live_answers', ['room_id'])
        op.create_index('ix_live_answers_team_id', 'live_answers', ['team_id'])


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    for table in ('live_answers', 'live_teams', 'live_rooms'):
        if table in tables:
            op.drop_table(table)
