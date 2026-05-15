"""tip_entries: schedule_id + per-schedule unique

Revision ID: b9547320555c
Revises: 162d17df3611
Create Date: 2026-05-14 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b9547320555c'
down_revision: Union[str, None] = '162d17df3611'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tip_entries',
        sa.Column('schedule_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_tip_entries_schedule_id',
        'tip_entries', 'schedules', ['schedule_id'], ['id'],
        ondelete='SET NULL',
    )
    op.drop_constraint('uq_tip_entry_employee_date_role', 'tip_entries', type_='unique')
    # Partial unique: 한 직원-schedule 1건. schedule_id NULL (매니저 freeform) 은 검사 안 함.
    op.create_index(
        'uq_tip_entry_employee_schedule',
        'tip_entries',
        ['employee_id', 'schedule_id'],
        unique=True,
        postgresql_where=sa.text('schedule_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_tip_entry_employee_schedule',
        table_name='tip_entries',
        postgresql_where=sa.text('schedule_id IS NOT NULL'),
    )
    op.create_unique_constraint(
        'uq_tip_entry_employee_date_role',
        'tip_entries',
        ['employee_id', 'store_id', 'work_role_id', 'date'],
    )
    op.drop_constraint('fk_tip_entries_schedule_id', 'tip_entries', type_='foreignkey')
    op.drop_column('tip_entries', 'schedule_id')
