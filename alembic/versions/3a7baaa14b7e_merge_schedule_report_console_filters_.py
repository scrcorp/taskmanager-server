"""merge schedule_report + console_filters heads

Revision ID: 3a7baaa14b7e
Revises: b56ec5fb26dc, c6d5109b68fc
Create Date: 2026-05-15 15:46:54.466927

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a7baaa14b7e'
down_revision: Union[str, None] = ('b56ec5fb26dc', 'c6d5109b68fc')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
