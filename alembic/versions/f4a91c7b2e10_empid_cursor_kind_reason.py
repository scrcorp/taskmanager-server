"""empid 채번 커서 + 번호 구분 + 이력 사유

- store_groups.next_empid / stores.next_empid: 채번 커서 (MAX(empid) 대체)
- org_member_stores.empid_kind: 'sequence' | 'exception'
- empid_changes.reason: 변경 사유 (커서 조정·재계산은 필수)

백필: 커서를 채번 스코프 단위로 계산해 전부 채운다. 코드에 NULL 폴백을 남기지
않기 위함(남기면 MAX 경로가 코드에 살아남는다). 여기서만 MAX(empid) 를 쓴다 —
INV-1 의 명시적 예외. 기존 행은 전부 sequence 로 본다(예외 분류는 도입 이후 값).

계산식은 현행 next_empid() 와 동일하게 max(MAX(empid)+1, floor) 이므로,
백필 직후 첫 발급 결과가 마이그레이션 전과 같다(INV-9).

floor 규칙(= app/services/org_numbering.py _empid_floor):
  - 그룹 공유 스코프: 그룹 number_range_start > 1 (매장 개별값 무시)
  - 매장 단독 스코프: 매장 number_range_start > 그룹값 > 1

Revision ID: f4a91c7b2e10
Revises: e3cbb0d54b55
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a91c7b2e10'
down_revision: Union[str, None] = 'e3cbb0d54b55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 백필 SQL — 테스트가 같은 문장을 재실행해 INV-9(백필 후 첫 발급 = 마이그레이션 전)를
# 검증할 수 있도록 상수로 뺀다. 여기서만 MAX(empid) 를 쓴다(INV-1 예외).
BACKFILL_GROUP_CURSOR_SQL = """
UPDATE store_groups g
   SET next_empid = GREATEST(
           COALESCE((
               SELECT MAX(oms.empid)
                 FROM org_member_stores oms
                 JOIN stores s ON s.id = oms.store_id
                WHERE s.group_id = g.id
           ), 0) + 1,
           COALESCE(g.number_range_start, 1)
       )
"""

BACKFILL_STORE_CURSOR_SQL = """
UPDATE stores s
   SET next_empid = GREATEST(
           COALESCE((
               SELECT MAX(oms.empid)
                 FROM org_member_stores oms
                WHERE oms.store_id = s.id
           ), 0) + 1,
           COALESCE(
               s.number_range_start,
               (SELECT g.number_range_start FROM store_groups g WHERE g.id = s.group_id),
               1
           )
       )
"""


def upgrade() -> None:
    # ── 1. 커서 컬럼 ──────────────────────────────────────────────
    op.add_column('store_groups', sa.Column('next_empid', sa.Integer(), nullable=True))
    op.add_column('stores', sa.Column('next_empid', sa.Integer(), nullable=True))

    # ── 2. 번호 구분 (기존 행은 server_default 로 전부 'sequence') ──
    op.add_column(
        'org_member_stores',
        sa.Column(
            'empid_kind', sa.String(length=20),
            nullable=False, server_default='sequence',
        ),
    )

    # ── 3. 이력 사유 ─────────────────────────────────────────────
    op.add_column('empid_changes', sa.Column('reason', sa.String(length=500), nullable=True))

    # ── 4. 커서 백필 ─────────────────────────────────────────────
    # 4-a. 그룹 커서 — 그룹 공유 스코프(numbering_mode='group')는 그룹 내 전 매장의
    #      MAX(empid)+1. 그룹 floor = 그룹 number_range_start.
    #      mode='store' 그룹도 함께 채운다 — 나중에 Shared 로 바꿔도 NULL 이 없도록
    #      (그 값은 shared 로 전환하기 전까지 쓰이지 않는다).
    op.execute(BACKFILL_GROUP_CURSOR_SQL)
    # 4-b. 매장 커서 — 매장 단독 스코프는 그 매장의 MAX(empid)+1.
    #      매장 floor = 매장 number_range_start > 그룹값 > 1.
    #      그룹 공유 매장의 커서는 지금은 쉬지만(그룹 커서를 쓴다), 그룹에서 빠지거나
    #      모드가 바뀌면 즉시 필요하므로 전 매장을 채운다.
    op.execute(BACKFILL_STORE_CURSOR_SQL)


def downgrade() -> None:
    op.drop_column('empid_changes', 'reason')
    op.drop_column('org_member_stores', 'empid_kind')
    op.drop_column('stores', 'next_empid')
    op.drop_column('store_groups', 'next_empid')
