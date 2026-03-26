"""merge inventory and register-store migrations

Revision ID: 936f0b67898a
Revises: 0f8f46005818, 9531e0822012
Create Date: 2026-03-26 15:12:12.537564

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '936f0b67898a'
down_revision: Union[str, None] = ('0f8f46005818', '9531e0822012')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
