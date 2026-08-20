"""payroll group scope — pay_periods store → store_group (D1)

급여 스코프를 매장에서 법인(store group)으로 옮긴다
(SoT: docs/99_inbox/2026-08-19-payroll-group-scope-전환-스펙.md):
    - pay_periods.store_group_id 신설 (RESTRICT — 확정 원장 보존)
    - store_id 는 nullable 로 낮춰 레거시(전환 전 확정) 행 전용으로 남긴다
    - **open(미확정) 기간은 삭제** — 동결 데이터가 없고(ensure_period 가
      온디맨드 재생성), store 스코프 open 행을 남기면 group 스코프 신규 행과
      같은 날짜에 이중으로 존재하게 된다. payroll_events.pay_period_id 는
      confirm 시에만 채워지므로 open 삭제로 끊길 참조가 없다 (FK 도 SET NULL).
    - payroll_entries.store_id 도 nullable — group 스코프 entry 는 매장 귀속이
      없다 (매장별 상세는 breakdown.days).

Revision ID: aa789ecf877b
Revises: 5def010ca5a7
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa789ecf877b'
down_revision: Union[str, None] = '5def010ca5a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 미확정(open) 기간 삭제 — group 스코프로 재생성된다 (docstring 참조).
    # 확정 기간은 동결 원장이라 store 스코프 그대로 보존.
    op.execute(sa.text("DELETE FROM pay_periods WHERE status != 'confirmed'"))

    op.add_column('pay_periods', sa.Column('store_group_id', sa.Uuid(), nullable=True))
    op.alter_column('pay_periods', 'store_id',
               existing_type=sa.UUID(),
               nullable=True)
    op.create_index(op.f('ix_pay_periods_store_group_id'), 'pay_periods', ['store_group_id'], unique=False)
    op.create_unique_constraint('uq_pay_period_group_start', 'pay_periods', ['store_group_id', 'start_date'])
    op.create_foreign_key(
        'fk_pay_periods_store_group_id',
        'pay_periods', 'store_groups', ['store_group_id'], ['id'],
        ondelete='RESTRICT',
    )
    op.alter_column('payroll_entries', 'store_id',
               existing_type=sa.UUID(),
               nullable=True)
    # ### end Alembic commands ###


def downgrade() -> None:
    # group 스코프 행은 store 스코프로 되돌릴 수 없다 — 삭제 (open 만 존재할
    # 것을 기대하지만, confirmed group 행이 있으면 entries 도 CASCADE 소멸 주의)
    op.execute(sa.text("DELETE FROM pay_periods WHERE store_group_id IS NOT NULL"))
    op.execute(sa.text("DELETE FROM payroll_entries WHERE store_id IS NULL"))
    op.alter_column('payroll_entries', 'store_id',
               existing_type=sa.UUID(),
               nullable=False)
    op.drop_constraint('fk_pay_periods_store_group_id', 'pay_periods', type_='foreignkey')
    op.drop_constraint('uq_pay_period_group_start', 'pay_periods', type_='unique')
    op.drop_index(op.f('ix_pay_periods_store_group_id'), table_name='pay_periods')
    op.alter_column('pay_periods', 'store_id',
               existing_type=sa.UUID(),
               nullable=False)
    op.drop_column('pay_periods', 'store_group_id')
    # ### end Alembic commands ###
