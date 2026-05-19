"""merge super_owner and tip_distribution_filter heads

Revision ID: 36913b3c9731
Revises: a73d51ebde13, ccc5116206d1
Create Date: 2026-05-19 14:54:57.253318

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36913b3c9731'
down_revision: Union[str, None] = ('a73d51ebde13', 'ccc5116206d1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
