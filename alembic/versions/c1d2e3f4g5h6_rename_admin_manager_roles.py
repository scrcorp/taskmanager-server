"""rename admin/manager roles to owner/general_manager

Revision ID: c1d2e3f4g5h6
Revises: b1c2d3e4f5g6
Create Date: 2026-02-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4g5h6"
down_revision: Union[str, None] = "b1c2d3e4f5g6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE roles SET name = 'owner' WHERE level = 1")
    op.execute("UPDATE roles SET name = 'general_manager' WHERE level = 2")


def downgrade() -> None:
    op.execute("UPDATE roles SET name = 'admin' WHERE level = 1")
    op.execute("UPDATE roles SET name = 'manager' WHERE level = 2")
