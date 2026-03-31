"""merge day_start_time hotfix and headcount JSONB

Revision ID: 0c47d6a6bca9
Revises: b5bd8d470225, f17bc5d7f162
Create Date: 2026-03-31 17:13:11.540014

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0c47d6a6bca9'
down_revision: Union[str, None] = ('b5bd8d470225', 'f17bc5d7f162')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
