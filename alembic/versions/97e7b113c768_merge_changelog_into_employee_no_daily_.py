"""merge changelog into employee_no daily-report heads

Revision ID: 97e7b113c768
Revises: 316128a85fd9, db52ad29a2f5
Create Date: 2026-06-29 19:57:18.749083

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '97e7b113c768'
down_revision: Union[str, None] = ('316128a85fd9', 'db52ad29a2f5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
