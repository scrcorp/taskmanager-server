"""add hourly_rate to schedule_requests

Revision ID: b1c2e3f4g5h7
Revises: aecdb454a37a
Create Date: 2026-03-30 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2e3f4g5h7'
down_revision: Union[str, None] = 'aecdb454a37a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'schedule_requests',
        sa.Column('hourly_rate', sa.Numeric(precision=10, scale=2), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('schedule_requests', 'hourly_rate')
