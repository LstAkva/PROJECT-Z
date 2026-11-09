"""add_profile_identity_and_privacy_fields

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-29 01:10:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'users' in tables:
        existing_cols = {c['name'] for c in inspector.get_columns('users')}

        if 'username' not in existing_cols:
            op.add_column('users', sa.Column('username', sa.String(length=50), nullable=True))
            op.create_index('ix_users_username', 'users', ['username'], unique=True)

        if 'avatar_url' not in existing_cols:
            op.add_column('users', sa.Column('avatar_url', sa.String(length=500), nullable=True))

        if 'bio' not in existing_cols:
            op.add_column('users', sa.Column('bio', sa.Text(), nullable=True))

        if 'is_public' not in existing_cols:
            op.add_column('users', sa.Column('is_public', sa.Boolean(), server_default='true', nullable=False))

        if 'show_stats' not in existing_cols:
            op.add_column('users', sa.Column('show_stats', sa.Boolean(), server_default='true', nullable=False))

        if 'theme_preference' not in existing_cols:
            op.add_column('users', sa.Column('theme_preference', sa.String(length=20), server_default='dark', nullable=False))

        if 'cookie_consent' not in existing_cols:
            op.add_column('users', sa.Column('cookie_consent', sa.String(length=50), server_default='necessary', nullable=False))

        if 'consent_recorded_at' not in existing_cols:
            op.add_column('users', sa.Column('consent_recorded_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'users' in tables:
        existing_cols = {c['name'] for c in inspector.get_columns('users')}

        for col in [
            'consent_recorded_at',
            'cookie_consent',
            'theme_preference',
            'show_stats',
            'is_public',
            'bio',
            'avatar_url',
        ]:
            if col in existing_cols:
                op.drop_column('users', col)

        if 'username' in existing_cols:
            try:
                op.drop_index('ix_users_username', table_name='users')
            except Exception:
                pass
            op.drop_column('users', 'username')
