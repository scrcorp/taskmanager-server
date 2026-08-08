"""merge dev email normalization with htma access code audits

Revision ID: 7250b506159c
Revises: a7c3e91b4d20, b8b6cf31612b
Create Date: 2026-08-07 22:56:22.506448

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7250b506159c'
down_revision: Union[str, None] = ('a7c3e91b4d20', 'b8b6cf31612b')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
