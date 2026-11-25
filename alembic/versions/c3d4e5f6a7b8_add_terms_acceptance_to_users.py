"""add_terms_acceptance_to_users

Records when a user agreed to the Privacy Policy and Terms of Use, and which dated version.
Both columns are nullable: accounts created before this change have no record.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-23 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'users' not in inspector.get_table_names():
        return
    existing_cols = {c['name'] for c in inspector.get_columns('users')}
    if 'terms_accepted_at' not in existing_cols:
        op.add_column('users', sa.Column('terms_accepted_at', sa.DateTime(), nullable=True))
    if 'terms_version' not in existing_cols:
        op.add_column('users', sa.Column('terms_version', sa.String(length=20), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'users' not in inspector.get_table_names():
        return
    existing_cols = {c['name'] for c in inspector.get_columns('users')}
    for col in ('terms_version', 'terms_accepted_at'):
        if col in existing_cols:
            op.drop_column('users', col)
