"""schedule_request_templates store_id nullable

Revision ID: 4c5d5bfe64d3
Revises: t1u2v3w4x5y6
Create Date: 2026-03-12 13:37:05.151248

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4c5d5bfe64d3'
down_revision: Union[str, None] = 't1u2v3w4x5y6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('schedule_request_templates', 'store_id',
               existing_type=sa.UUID(),
               nullable=True)


def downgrade() -> None:
    op.alter_column('schedule_request_templates', 'store_id',
               existing_type=sa.UUID(),
               nullable=False)
