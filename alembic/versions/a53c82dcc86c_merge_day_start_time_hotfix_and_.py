"""merge day_start_time hotfix and headcount JSONB

Revision ID: a53c82dcc86c
Revises: 0c47d6a6bca9
Create Date: 2026-03-31 17:16:20.701869

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a53c82dcc86c'
down_revision: Union[str, None] = '0c47d6a6bca9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
