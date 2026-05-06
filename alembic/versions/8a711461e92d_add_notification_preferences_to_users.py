"""add notification_preferences to users

Revision ID: 8a711461e92d
Revises: f426c3548271
Create Date: 2026-05-04 17:41:03.523161

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8a711461e92d'
down_revision: Union[str, None] = 'f426c3548271'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'notification_preferences',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='{}',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'notification_preferences')
