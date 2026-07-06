"""merge files+store_status heads

Revision ID: 53e89bce7714
Revises: a6e54e9c90de, a82888275180
Create Date: 2026-06-29 15:08:05.124551

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '53e89bce7714'
down_revision: Union[str, None] = ('a6e54e9c90de', 'a82888275180')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
