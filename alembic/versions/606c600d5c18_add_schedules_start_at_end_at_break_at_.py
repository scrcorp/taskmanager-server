"""add schedules start_at/end_at/break_at datetime columns

Revision ID: 606c600d5c18
Revises: 028cfe3a6b1c
Create Date: 2026-07-09 16:40:30.532660

벽시계 datetime 인코딩(start_at/end_at/break_*_at) 컬럼 추가 + 기존 *_time에서 백필.
백필은 현재 해석을 그대로 복사한다(재해석 없음):
  - start_at = work_date + start_time
  - end_at   = work_date + end_time (+1일 if end_time <= start_time, 자정 넘김)
  - break_*_at = work_date + break_*_time (+1일 if break < start_time, 오버나잇 근무 내 브레이크)
전환기(Wave 1): 기존 *_time 컬럼은 유지. Wave 3에서 제거 예정.

주의: autogenerate가 무관한 스키마 드리프트(다른 테이블 인덱스/컬럼)를 대량 감지했으나
이 마이그레이션은 schedules 컬럼 추가 + 백필로만 한정한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '606c600d5c18'
down_revision: Union[str, None] = '028cfe3a6b1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schedules', sa.Column('start_at', sa.DateTime(), nullable=True))
    op.add_column('schedules', sa.Column('end_at', sa.DateTime(), nullable=True))
    op.add_column('schedules', sa.Column('break_start_at', sa.DateTime(), nullable=True))
    op.add_column('schedules', sa.Column('break_end_at', sa.DateTime(), nullable=True))

    # Backfill — 현재 해석을 그대로 복사 (date + time → timestamp)
    op.execute("""
        UPDATE schedules
        SET start_at = (work_date + start_time)
        WHERE start_time IS NOT NULL
    """)
    op.execute("""
        UPDATE schedules
        SET end_at = (work_date + end_time)
                   + (CASE WHEN start_time IS NOT NULL AND end_time <= start_time
                           THEN INTERVAL '1 day' ELSE INTERVAL '0 day' END)
        WHERE end_time IS NOT NULL
    """)
    op.execute("""
        UPDATE schedules
        SET break_start_at = (work_date + break_start_time)
                           + (CASE WHEN start_time IS NOT NULL AND break_start_time < start_time
                                   THEN INTERVAL '1 day' ELSE INTERVAL '0 day' END)
        WHERE break_start_time IS NOT NULL
    """)
    op.execute("""
        UPDATE schedules
        SET break_end_at = (work_date + break_end_time)
                         + (CASE WHEN start_time IS NOT NULL AND break_end_time < start_time
                                 THEN INTERVAL '1 day' ELSE INTERVAL '0 day' END)
        WHERE break_end_time IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_column('schedules', 'break_end_at')
    op.drop_column('schedules', 'break_start_at')
    op.drop_column('schedules', 'end_at')
    op.drop_column('schedules', 'start_at')
