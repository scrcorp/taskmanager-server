"""task comment attachments column

Revision ID: 0b49bd3c76e3
Revises: 3e96032d6c76
Create Date: 2026-05-18 22:00:21.460145
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0b49bd3c76e3'
down_revision: Union[str, None] = '3e96032d6c76'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'task_comments',
        sa.Column(
            'attachments',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='[]',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('task_comments', 'attachments')
