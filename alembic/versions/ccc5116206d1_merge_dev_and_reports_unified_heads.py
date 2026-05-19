"""merge dev and reports-unified heads

Revision ID: ccc5116206d1
Revises: 0ecfa8fac48e, 19ccbdbb9e16
Create Date: 2026-05-19 11:58:12.467497

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ccc5116206d1'
down_revision: Union[str, None] = ('0ecfa8fac48e', '19ccbdbb9e16')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
