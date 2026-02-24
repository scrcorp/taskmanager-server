"""add_schedules

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-02-24 18:00:00.000000

스케줄(schedules) 및 스케줄 승인 이력(schedule_approvals) 테이블 생성.
매장(stores)에 승인 필요 여부(require_approval) 컬럼 추가.
Add schedules and schedule_approvals tables.
Add require_approval column to stores table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # schedules — 스케줄 초안 (SV가 작성, GM이 승인)
    # Schedule drafts (created by SV, approved by GM)
    op.create_table(
        'schedules',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('shift_id', UUID(as_uuid=True), sa.ForeignKey('shifts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('position_id', UUID(as_uuid=True), sa.ForeignKey('positions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('work_date', sa.Date(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('end_time', sa.Time(), nullable=True),
        sa.Column('status', sa.String(20), server_default='draft', nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_by', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_by', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('work_assignment_id', UUID(as_uuid=True), sa.ForeignKey('work_assignments.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 인덱스 — Indexes
    op.create_index('ix_schedules_org_store_date', 'schedules', ['organization_id', 'store_id', 'work_date'])
    op.create_index('ix_schedules_user_date', 'schedules', ['user_id', 'work_date'])

    # 유니크 제약 — Unique constraint: 동일 사용자+매장+날짜+시프트 중복 방지
    # Prevent duplicate scheduling for user+store+date+shift
    op.create_unique_constraint(
        'uq_schedule_user_store_date_shift',
        'schedules',
        ['user_id', 'store_id', 'work_date', 'shift_id'],
    )

    # schedule_approvals — 승인 이력 (audit trail)
    # Schedule approval records for audit trail
    op.create_table(
        'schedule_approvals',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('schedule_id', UUID(as_uuid=True), sa.ForeignKey('schedules.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # stores 테이블에 승인 필요 여부 컬럼 추가
    # Add require_approval column to stores table
    op.add_column('stores', sa.Column('require_approval', sa.Boolean(), server_default=sa.text('true'), nullable=False))


def downgrade() -> None:
    # stores에서 require_approval 컬럼 제거
    # Remove require_approval column from stores
    op.drop_column('stores', 'require_approval')

    # schedule_approvals 테이블 삭제
    # Drop schedule_approvals table
    op.drop_table('schedule_approvals')

    # schedules 테이블 삭제 (인덱스, 유니크 제약은 테이블과 함께 삭제됨)
    # Drop schedules table (indexes and constraints are dropped with the table)
    op.drop_table('schedules')
