"""add_attendance

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-02-24 20:00:00.000000

근태 관리 테이블 생성: qr_codes, attendances, attendance_corrections.
Add attendance management tables: qr_codes, attendances, attendance_corrections.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # qr_codes — 매장별 QR 코드 (one active per store)
    # QR codes per store for attendance scanning
    op.create_table(
        'qr_codes',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('code', sa.String(64), nullable=False, unique=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_by', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    )

    # QR 코드 인덱스 — QR code indexes
    op.create_index('ix_qr_codes_store', 'qr_codes', ['store_id'])

    # attendances — 근태 기록 (one record per user per work date)
    # Attendance records for daily clock-in/out tracking
    op.create_table(
        'attendances',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('work_date', sa.Date(), nullable=False),
        sa.Column('clock_in', sa.DateTime(timezone=True), nullable=True),
        sa.Column('clock_in_timezone', sa.String(50), nullable=True),
        sa.Column('break_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('break_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('clock_out', sa.DateTime(timezone=True), nullable=True),
        sa.Column('clock_out_timezone', sa.String(50), nullable=True),
        sa.Column('status', sa.String(20), server_default='clocked_in', nullable=False),
        sa.Column('total_work_minutes', sa.Integer(), nullable=True),
        sa.Column('total_break_minutes', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 근태 인덱스 — Attendance indexes
    op.create_index('ix_attendances_org_store_date', 'attendances', ['organization_id', 'store_id', 'work_date'])
    op.create_index('ix_attendances_user_date', 'attendances', ['user_id', 'work_date'])

    # 유니크 제약 — Unique constraint: 동일 사용자+날짜 중복 방지
    # Prevent duplicate attendance for same user on same day
    op.create_unique_constraint(
        'uq_attendance_user_date',
        'attendances',
        ['user_id', 'work_date'],
    )

    # attendance_corrections — 근태 수정 이력
    # Attendance correction audit trail
    op.create_table(
        'attendance_corrections',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('attendance_id', UUID(as_uuid=True), sa.ForeignKey('attendances.id', ondelete='CASCADE'), nullable=False),
        sa.Column('field_name', sa.String(50), nullable=False),
        sa.Column('original_value', sa.Text(), nullable=True),
        sa.Column('corrected_value', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('corrected_by', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    # attendance_corrections 테이블 삭제
    # Drop attendance_corrections table
    op.drop_table('attendance_corrections')

    # attendances 테이블 삭제 (인덱스, 유니크 제약은 테이블과 함께 삭제됨)
    # Drop attendances table (indexes and constraints are dropped with the table)
    op.drop_table('attendances')

    # qr_codes 테이블 삭제
    # Drop qr_codes table
    op.drop_table('qr_codes')
