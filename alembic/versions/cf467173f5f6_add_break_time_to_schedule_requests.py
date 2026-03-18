"""add break time to schedule_requests

Revision ID: cf467173f5f6
Revises: 50ae06dae166
Create Date: 2026-03-13 10:56:48.615514

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'cf467173f5f6'
down_revision: Union[str, None] = '50ae06dae166'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schedule_requests', sa.Column('break_start_time', sa.Time(), nullable=True))
    op.add_column('schedule_requests', sa.Column('break_end_time', sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column('schedule_requests', 'break_end_time')
    op.drop_column('schedule_requests', 'break_start_time')
