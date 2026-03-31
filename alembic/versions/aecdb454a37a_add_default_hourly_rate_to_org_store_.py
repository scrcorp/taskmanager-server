"""add default_hourly_rate to org/store, rename schedule hourly_rate

Revision ID: aecdb454a37a
Revises: 628e82e4319b
Create Date: 2026-03-30 16:49:35.058465

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aecdb454a37a'
down_revision: Union[str, None] = '628e82e4319b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # org/store default hourly rate
    op.add_column('organizations', sa.Column('default_hourly_rate', sa.Numeric(precision=10, scale=2), nullable=False, server_default='0'))
    op.add_column('stores', sa.Column('default_hourly_rate', sa.Numeric(precision=10, scale=2), nullable=True))

    # Fill null values before rename + not-null
    op.execute("UPDATE schedules SET hourly_rate_override = 0 WHERE hourly_rate_override IS NULL")
    # Rename schedule.hourly_rate_override → schedule.hourly_rate (preserve data)
    op.alter_column('schedules', 'hourly_rate_override',
                     new_column_name='hourly_rate',
                     existing_type=sa.Numeric(precision=10, scale=2),
                     nullable=False,
                     server_default='0')


def downgrade() -> None:
    op.alter_column('schedules', 'hourly_rate',
                     new_column_name='hourly_rate_override',
                     existing_type=sa.Numeric(precision=10, scale=2),
                     nullable=True,
                     server_default=None)
    op.drop_column('stores', 'default_hourly_rate')
    op.drop_column('organizations', 'default_hourly_rate')
