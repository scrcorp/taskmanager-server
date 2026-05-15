"""add tip_periods

Revision ID: 1d29a3cb3a8e
Revises: b9547320555c
Create Date: 2026-05-14 17:30:13.769261

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '1d29a3cb3a8e'
down_revision: Union[str, None] = 'b9547320555c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tip_periods',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_by', sa.Uuid(), nullable=True),
        sa.Column('override_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['confirmed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('store_id', 'start_date', 'end_date', name='uq_tip_period_store_range'),
    )
    op.create_index('ix_tip_periods_store_dates', 'tip_periods', ['store_id', 'start_date'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_tip_periods_store_dates', table_name='tip_periods')
    op.drop_table('tip_periods')
