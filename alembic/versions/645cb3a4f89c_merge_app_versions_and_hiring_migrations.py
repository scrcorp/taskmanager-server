"""merge app_versions and hiring migrations

Revision ID: 645cb3a4f89c
Revises: b476eab64f72, e391353fff41
Create Date: 2026-04-29 17:49:03.156633

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '645cb3a4f89c'
down_revision: Union[str, None] = ('b476eab64f72', 'e391353fff41')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
