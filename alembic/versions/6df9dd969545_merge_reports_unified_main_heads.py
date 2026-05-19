"""merge reports-unified + main heads

Revision ID: 6df9dd969545
Revises: a976607915c3, ce5041619d76
Create Date: 2026-05-18 12:04:57.174128

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6df9dd969545'
down_revision: Union[str, None] = ('a976607915c3', 'ce5041619d76')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
