"""drop work_assignments 테이블, cl_instances.work_assignment_id 컬럼 제거

Revision ID: af48864a7d30
Revises: 870805b3e3bc
Create Date: 2026-03-13 18:38:30.266233

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'af48864a7d30'
down_revision: Union[str, None] = '870805b3e3bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. cl_instances에서 work_assignment_id FK/컬럼 제거
    op.drop_constraint('cl_instances_work_assignment_id_key', 'cl_instances', type_='unique')
    op.drop_constraint('cl_instances_work_assignment_id_fkey', 'cl_instances', type_='foreignkey')
    op.drop_column('cl_instances', 'work_assignment_id')

    # 2. work_assignments 테이블 삭제
    op.drop_table('work_assignments')


def downgrade() -> None:
    # 1. work_assignments 테이블 복원
    op.create_table('work_assignments',
        sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
        sa.Column('organization_id', sa.UUID(), autoincrement=False, nullable=False),
        sa.Column('store_id', sa.UUID(), autoincrement=False, nullable=False),
        sa.Column('shift_id', sa.UUID(), autoincrement=False, nullable=False),
        sa.Column('position_id', sa.UUID(), autoincrement=False, nullable=False),
        sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
        sa.Column('work_date', sa.DATE(), autoincrement=False, nullable=False),
        sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
        sa.Column('checklist_snapshot', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
        sa.Column('total_items', sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column('completed_items', sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column('assigned_by', sa.UUID(), autoincrement=False, nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(['assigned_by'], ['users.id'], name='work_assignments_assigned_by_fkey'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='work_assignments_organization_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['position_id'], ['positions.id'], name='work_assignments_position_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shift_id'], ['shifts.id'], name='work_assignments_shift_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], name='work_assignments_brand_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='work_assignments_user_id_fkey', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='work_assignments_pkey'),
        sa.UniqueConstraint('store_id', 'shift_id', 'position_id', 'user_id', 'work_date', name='uq_assignment_combo_date')
    )

    # 2. cl_instances에 work_assignment_id 컬럼 복원
    op.add_column('cl_instances', sa.Column('work_assignment_id', sa.UUID(), autoincrement=False, nullable=True))
    op.create_foreign_key('cl_instances_work_assignment_id_fkey', 'cl_instances', 'work_assignments', ['work_assignment_id'], ['id'], ondelete='SET NULL')
    op.create_unique_constraint('cl_instances_work_assignment_id_key', 'cl_instances', ['work_assignment_id'])
