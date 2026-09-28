"""add_anon_unique_index_and_metadata

Revision ID: 9c0d1e2f3a4b
Revises: 8b9c0d1e2f3a
Create Date: 2026-09-20 00:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9c0d1e2f3a4b'
down_revision: Union[str, Sequence[str], None] = '8b9c0d1e2f3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Add attempt_metadata to solo_attempts if missing
    columns = [c['name'] for c in inspector.get_columns('solo_attempts')]
    if 'attempt_metadata' not in columns:
        op.add_column(
            'solo_attempts',
            sa.Column(
                'attempt_metadata',
                postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'),
                nullable=True,
            ),
        )

    # 2. Add partial unique index on solo_attempts.anon_id (WHERE anon_id IS NOT NULL)
    indexes = [idx['name'] for idx in inspector.get_indexes('solo_attempts')]
    if 'idx_solo_attempts_anon_id_unique' not in indexes:
        op.create_index(
            'idx_solo_attempts_anon_id_unique',
            'solo_attempts',
            ['anon_id'],
            unique=True,
            postgresql_where=sa.text('anon_id IS NOT NULL'),
            sqlite_where=sa.text('anon_id IS NOT NULL'),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    indexes = [idx['name'] for idx in inspector.get_indexes('solo_attempts')]
    if 'idx_solo_attempts_anon_id_unique' in indexes:
        op.drop_index('idx_solo_attempts_anon_id_unique', table_name='solo_attempts')

    columns = [c['name'] for c in inspector.get_columns('solo_attempts')]
    if 'attempt_metadata' in columns:
        op.drop_column('solo_attempts', 'attempt_metadata')
