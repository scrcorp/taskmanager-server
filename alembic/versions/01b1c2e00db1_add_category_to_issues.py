"""add category to issues

Revision ID: 01b1c2e00db1
Revises: 6df9dd969545
Create Date: 2026-05-18 17:03:29.854106

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '01b1c2e00db1'
down_revision: Union[str, None] = '6df9dd969545'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'issues',
        sa.Column('category', sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('issues', 'category')
