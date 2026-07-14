"""staff work availability + history

Revision ID: 1abb4bb59226
Revises: 3d4919b8a17e
Create Date: 2026-07-13 14:56:54.541126

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1abb4bb59226'
down_revision: Union[str, None] = '3d4919b8a17e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'staff_availability',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('day_of_week', sa.SmallInteger(), nullable=False),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('end_time', sa.Time(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('updated_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(state = 'range' AND start_time IS NOT NULL AND end_time IS NOT NULL AND end_time <> start_time)"
            " OR (state IN ('off', 'full') AND start_time IS NULL AND end_time IS NULL)",
            name='ck_staff_availability_times',
        ),
        sa.CheckConstraint("source IN ('console_manager', 'staff_self')", name='ck_staff_availability_source'),
        sa.CheckConstraint("state IN ('off', 'range', 'full')", name='ck_staff_availability_state'),
        sa.CheckConstraint('day_of_week >= 0 AND day_of_week <= 6', name='ck_staff_availability_dow'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'organization_id', 'day_of_week', name='uq_staff_availability_user_org_dow'),
    )
    op.create_index(op.f('ix_staff_availability_organization_id'), 'staff_availability', ['organization_id'], unique=False)
    op.create_index(op.f('ix_staff_availability_user_id'), 'staff_availability', ['user_id'], unique=False)

    op.create_table(
        'staff_availability_history',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('day_of_week', sa.SmallInteger(), nullable=True),
        sa.Column('actor_id', sa.Uuid(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('prev', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('console_manager', 'staff_self')", name='ck_staff_availability_history_source'),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_staff_availability_history_organization_id'), 'staff_availability_history', ['organization_id'], unique=False)
    op.create_index(op.f('ix_staff_availability_history_user_id'), 'staff_availability_history', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_staff_availability_history_user_id'), table_name='staff_availability_history')
    op.drop_index(op.f('ix_staff_availability_history_organization_id'), table_name='staff_availability_history')
    op.drop_table('staff_availability_history')
    op.drop_index(op.f('ix_staff_availability_user_id'), table_name='staff_availability')
    op.drop_index(op.f('ix_staff_availability_organization_id'), table_name='staff_availability')
    op.drop_table('staff_availability')
