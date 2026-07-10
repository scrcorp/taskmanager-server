"""add schedules.operating_day (rename work_date, create-then-delete part 1)

Revision ID: 7a1c9d2e4f60
Revises: 606c600d5c18
Create Date: 2026-07-09 17:05:00.000000

work_date → operating_day 리네이밍의 1단계(create-then-delete).
operating_day = "이 근무가 귀속·표시되는 영업일 라벨"(물리적 시각 아님).
전환기(Wave 1): work_date와 operating_day 공존, 쓰기 시 동기화.
Wave 3 정리 단계에서 work_date 및 그 인덱스 제거 + operating_day NOT NULL화 예정.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7a1c9d2e4f60'
down_revision: Union[str, None] = '606c600d5c18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schedules', sa.Column('operating_day', sa.Date(), nullable=True))
    op.execute("UPDATE schedules SET operating_day = work_date")
    # work_date 인덱스를 operating_day로도 미러링 (전환기 쿼리 성능 유지)
    op.create_index('ix_schedules_org_store_opday', 'schedules',
                    ['organization_id', 'store_id', 'operating_day'], unique=False)
    op.create_index('ix_schedules_user_opday', 'schedules',
                    ['user_id', 'operating_day'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_schedules_user_opday', table_name='schedules')
    op.drop_index('ix_schedules_org_store_opday', table_name='schedules')
    op.drop_column('schedules', 'operating_day')
