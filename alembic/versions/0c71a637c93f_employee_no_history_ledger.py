"""employee_no_history ledger

Revision ID: 0c71a637c93f
Revises: a82888275180
Create Date: 2026-06-29 16:42:32.321271

org별 사번 영구 burn 대장(append-only ledger) 생성 + 기존 부여분 백필.

옵션 A(영구 burn): org 안에서 한 번이라도 사용된 사번은 영구히 예약되어
누구에게도(본인 포함) 재부여 불가. 현 `users.employee_no` 부여분도 burn 목록에 포함.

NOTE: autogenerate 가 잡아낸 무관한 drift(notifications/announcements 등 과거 모델
정리 잔재)는 본 마이그레이션 범위가 아니므로 의도적으로 제외함.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0c71a637c93f'
down_revision: Union[str, None] = 'a82888275180'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'employee_no_history',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('employee_no', sa.String(length=50), nullable=False),
        sa.Column('first_assigned_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['first_assigned_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'employee_no', name='uq_emp_no_history_org_no'),
    )
    op.create_index('ix_emp_no_history_org', 'employee_no_history', ['organization_id'], unique=False)

    # 백필 — 현재 users.employee_no(NOT NULL) 부여분을 ledger 에 적재(burn).
    # 기존 데이터는 partial unique index 로 (org, employee_no) 가 유일하므로 충돌 없음.
    op.execute(
        """
        INSERT INTO employee_no_history (id, organization_id, employee_no, first_assigned_user_id, created_at)
        SELECT gen_random_uuid(), organization_id, employee_no, id, now()
        FROM users
        WHERE employee_no IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index('ix_emp_no_history_org', table_name='employee_no_history')
    op.drop_table('employee_no_history')
