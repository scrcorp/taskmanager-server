"""add schedule_report_snapshots

Revision ID: b56ec5fb26dc
Revises: 5fa05f743f22
Create Date: 2026-05-15 11:06:03.467090

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b56ec5fb26dc'
down_revision: Union[str, None] = '5fa05f743f22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'schedule_report_snapshots',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('target_date_from', sa.Date(), nullable=False),
        sa.Column('target_date_to', sa.Date(), nullable=False),
        sa.Column('issues', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_schedule_report_snapshots_org_sent',
        'schedule_report_snapshots',
        ['organization_id', 'sent_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_schedule_report_snapshots_org_sent', table_name='schedule_report_snapshots')
    op.drop_table('schedule_report_snapshots')
