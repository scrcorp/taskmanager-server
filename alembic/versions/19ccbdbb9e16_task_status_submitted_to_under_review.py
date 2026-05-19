"""task status submitted to under_review

Revision ID: 19ccbdbb9e16
Revises: 1b1c22938f1e
Create Date: 2026-05-19 10:36:55.495938

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '19ccbdbb9e16'
down_revision: Union[str, None] = '1b1c22938f1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # tasks.status: 'submitted' → 'under_review'
    op.execute(
        "UPDATE tasks SET status = 'under_review' WHERE status = 'submitted'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET status = 'submitted' WHERE status = 'under_review'"
    )
