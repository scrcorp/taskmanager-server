"""payroll phase1: hourly_rate_history + org_members hr fields

Revision ID: b77039450fd5
Revises: 66760e5e6178
Create Date: 2026-08-03 18:40:47.291453

Payroll v1 Phase 1 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md):
    1. hourly_rate_history 테이블 신설 — 개인 시급 변경 이력 (org_member RESTRICT)
    2. org_members 에 hire_date / termination_date 추가
    3. backfill: org_members.hourly_rate := users.hourly_rate
       (org_members 는 생성 시 1회 복사 후 미갱신 상태 — canonical 전환 전
       users 의 현재값을 그대로(NULL 포함) 재복사. 배포 서버에서도 동일 동작.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b77039450fd5'
down_revision: Union[str, None] = '818ff86ac8ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) hourly_rate_history — 시급 변경 이력 (급여 근거 데이터)
    op.create_table('hourly_rate_history',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('org_member_id', sa.Uuid(), nullable=False),
    sa.Column('old_rate', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('new_rate', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('changed_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('new_rate > 0', name='ck_rate_history_new_rate_positive'),
    sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_member_id'], ['org_members.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_member_id', 'effective_date', name='uq_rate_history_member_effective')
    )
    op.create_index(op.f('ix_hourly_rate_history_organization_id'), 'hourly_rate_history', ['organization_id'], unique=False)
    op.create_index('ix_rate_history_member_effective', 'hourly_rate_history', ['org_member_id', sa.text('effective_date DESC')], unique=False)

    # 2) org_members 고용 시작/종료일
    op.add_column('org_members', sa.Column('hire_date', sa.Date(), nullable=True))
    op.add_column('org_members', sa.Column('termination_date', sa.Date(), nullable=True))

    # 3) backfill — org_members.hourly_rate 를 users.hourly_rate 현재값으로 재복사.
    #    users 값 그대로 복사 (NULL 이면 NULL — org 기본값 사용 의미 유지).
    op.execute(
        """
        UPDATE org_members om
        SET hourly_rate = u.hourly_rate
        FROM users u
        WHERE om.user_id = u.id
        """
    )


def downgrade() -> None:
    # backfill 은 비파괴 복사라 별도 되돌림 없음 (원본 users.hourly_rate 는 그대로)
    op.drop_column('org_members', 'termination_date')
    op.drop_column('org_members', 'hire_date')
    op.drop_index('ix_rate_history_member_effective', table_name='hourly_rate_history')
    op.drop_index(op.f('ix_hourly_rate_history_organization_id'), table_name='hourly_rate_history')
    op.drop_table('hourly_rate_history')
