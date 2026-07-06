"""merge files head into employee_no+daily_report+changelog

Revision ID: 7adfe9e609ba
Revises: 53e89bce7714, 97e7b113c768
Create Date: 2026-06-30 10:22:41.756985

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7adfe9e609ba'
down_revision: Union[str, None] = ('53e89bce7714', '97e7b113c768')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
