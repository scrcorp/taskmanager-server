"""add refresh token grace window columns

Revision ID: 0ecfa8fac48e
Revises: ce5041619d76
Create Date: 2026-05-18 15:49:39.524248

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0ecfa8fac48e'
down_revision: Union[str, None] = 'ce5041619d76'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('refresh_tokens', sa.Column('replaced_by_token', sa.String(length=512), nullable=True))
    op.add_column('refresh_tokens', sa.Column('replaced_access_token', sa.String(length=1024), nullable=True))
    op.add_column('refresh_tokens', sa.Column('replaced_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('refresh_tokens', 'replaced_at')
    op.drop_column('refresh_tokens', 'replaced_access_token')
    op.drop_column('refresh_tokens', 'replaced_by_token')
