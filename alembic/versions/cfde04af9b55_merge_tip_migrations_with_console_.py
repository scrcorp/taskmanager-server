"""merge tip migrations with console_filters

Revision ID: cfde04af9b55
Revises: c6d5109b68fc, e0360eec700e
Create Date: 2026-05-15 16:04:48.668248

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cfde04af9b55'
down_revision: Union[str, None] = ('c6d5109b68fc', 'e0360eec700e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
