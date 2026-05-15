"""merge schedule_report with tip heads

Revision ID: ce5041619d76
Revises: cfde04af9b55, 3a7baaa14b7e
Create Date: 2026-05-15 17:02:01.560611

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce5041619d76'
down_revision: Union[str, None] = ('cfde04af9b55', '3a7baaa14b7e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
