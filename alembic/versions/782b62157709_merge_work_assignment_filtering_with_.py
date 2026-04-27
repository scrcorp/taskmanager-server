"""merge_work_assignment_filtering_with_attendance_device

Revision ID: 782b62157709
Revises: b38ea3c44b97, eaadd6aa7b9f
Create Date: 2026-04-24 10:32:23.022458

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '782b62157709'
down_revision: Union[str, None] = ('b38ea3c44b97', 'eaadd6aa7b9f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
