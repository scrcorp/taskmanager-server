"""add tip entries, distributions, audit logs

Revision ID: 162d17df3611
Revises: 5fa05f743f22
Create Date: 2026-05-14 11:30:20.688585

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '162d17df3611'
down_revision: Union[str, None] = '5fa05f743f22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tip_audit_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('entity_type', sa.String(length=30), nullable=False),
        sa.Column('entity_id', sa.Uuid(), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('actor_id', sa.Uuid(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tip_audit_actor_created', 'tip_audit_logs', ['actor_id', 'created_at'], unique=False)
    op.create_index('ix_tip_audit_entity', 'tip_audit_logs', ['entity_type', 'entity_id'], unique=False)

    op.create_table(
        'tip_entries',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=False),
        sa.Column('employee_id', sa.Uuid(), nullable=False),
        sa.Column('work_role_id', sa.Uuid(), nullable=True),
        sa.Column('work_role_name_snapshot', sa.String(length=100), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('card_tips', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('cash_tips_kept', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('last_modified_by_id', sa.Uuid(), nullable=True),
        sa.Column('last_modified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['employee_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['last_modified_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['work_role_id'], ['store_work_roles.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('employee_id', 'store_id', 'work_role_id', 'date', name='uq_tip_entry_employee_date_role'),
    )
    op.create_index('ix_tip_entries_employee_date', 'tip_entries', ['employee_id', 'date'], unique=False)
    op.create_index('ix_tip_entries_store_date', 'tip_entries', ['store_id', 'date'], unique=False)

    op.create_table(
        'tip_distributions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('entry_id', sa.Uuid(), nullable=False),
        sa.Column('receiver_id', sa.Uuid(), nullable=True),
        sa.Column('receiver_name_snapshot', sa.String(length=200), nullable=True),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('reason', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('pending_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['entry_id'], ['tip_entries.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['receiver_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tip_distributions_entry', 'tip_distributions', ['entry_id'], unique=False)
    op.create_index('ix_tip_distributions_pending_until', 'tip_distributions', ['pending_until'], unique=False)
    op.create_index('ix_tip_distributions_receiver_status', 'tip_distributions', ['receiver_id', 'status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_tip_distributions_receiver_status', table_name='tip_distributions')
    op.drop_index('ix_tip_distributions_pending_until', table_name='tip_distributions')
    op.drop_index('ix_tip_distributions_entry', table_name='tip_distributions')
    op.drop_table('tip_distributions')
    op.drop_index('ix_tip_entries_store_date', table_name='tip_entries')
    op.drop_index('ix_tip_entries_employee_date', table_name='tip_entries')
    op.drop_table('tip_entries')
    op.drop_index('ix_tip_audit_entity', table_name='tip_audit_logs')
    op.drop_index('ix_tip_audit_actor_created', table_name='tip_audit_logs')
    op.drop_table('tip_audit_logs')
