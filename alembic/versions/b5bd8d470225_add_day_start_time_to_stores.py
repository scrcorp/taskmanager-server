"""add day_start_time to stores

Revision ID: b5bd8d470225
Revises: 936f0b67898a
Create Date: 2026-03-31 13:36:39.269409

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b5bd8d470225'
down_revision: Union[str, None] = '936f0b67898a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('stores', sa.Column('day_start_time', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('stores', 'day_start_time')
