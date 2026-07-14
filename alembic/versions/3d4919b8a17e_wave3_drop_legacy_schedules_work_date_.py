"""wave3 drop legacy schedules work_date and time columns

Revision ID: 3d4919b8a17e
Revises: 7a1c9d2e4f60
Create Date: 2026-07-11 07:06:38.781970

Wave 3 — 스케줄 datetime 전환의 정리 단계.
전제: Wave 1+2 배포 완료 + 모든 쓰기 경로가 신 인코딩(operating_day + start_at/end_at)을 채움.
구 컬럼은 신 컬럼의 순수 투영이었으므로(work_date=operating_day, start_time=start_at::time)
정보 손실 없음. API 응답의 구 필드는 모델 read-only 프로퍼티 shim이 계속 방출(옛 앱 호환, D2).

리허설 검증(2026-07-10): dev 클론+엣지 시딩 4080행에서 upgrade/downgrade/재upgrade 사이클 clean.
⚠️ autogenerate가 감지한 무관 드리프트(announcements/notifications drop 등)는 수술로 제거함.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3d4919b8a17e'
down_revision: Union[str, None] = '7a1c9d2e4f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 안전장치: operating_day NULL 잔존 시 work_date로 최종 백필 (Wave1 백필 누락 대비)
    op.execute("UPDATE schedules SET operating_day = work_date WHERE operating_day IS NULL")

    # 2) operating_day NOT NULL 승격 (전환기 nullable 해제)
    op.alter_column('schedules', 'operating_day', existing_type=sa.DATE(), nullable=False)

    # 3) work_date 인덱스 제거 (operating_day 미러 인덱스가 대체)
    op.drop_index('ix_schedules_org_store_date', table_name='schedules')
    op.drop_index('ix_schedules_user_date', table_name='schedules')

    # 4) 구 컬럼 제거 — 날짜 없는 Time / 구 라벨. start_at/end_at/break_*_at 이 SoT.
    op.drop_column('schedules', 'break_end_time')
    op.drop_column('schedules', 'break_start_time')
    op.drop_column('schedules', 'end_time')
    op.drop_column('schedules', 'start_time')
    op.drop_column('schedules', 'work_date')


def downgrade() -> None:
    # 롤백: 구 컬럼 재생성 후 신 컬럼에서 역백필.
    # ⚠️ 손실: 새벽 +1일 시프트(start_at::date ≠ operating_day)는 구 인코딩으로
    #    표현 불가 → work_date=operating_day, start_time=start_at::time 로 평탄화(옛 동작).
    op.add_column('schedules', sa.Column('work_date', sa.DATE(), nullable=True))
    op.add_column('schedules', sa.Column('start_time', sa.Time(), nullable=True))
    op.add_column('schedules', sa.Column('end_time', sa.Time(), nullable=True))
    op.add_column('schedules', sa.Column('break_start_time', sa.Time(), nullable=True))
    op.add_column('schedules', sa.Column('break_end_time', sa.Time(), nullable=True))
    op.execute("""
        UPDATE schedules SET
            work_date = operating_day,
            start_time = start_at::time,
            end_time = end_at::time,
            break_start_time = break_start_at::time,
            break_end_time = break_end_at::time
    """)
    op.alter_column('schedules', 'work_date', nullable=False)
    op.create_index('ix_schedules_org_store_date', 'schedules',
                    ['organization_id', 'store_id', 'work_date'], unique=False)
    op.create_index('ix_schedules_user_date', 'schedules', ['user_id', 'work_date'], unique=False)
    op.alter_column('schedules', 'operating_day', existing_type=sa.DATE(), nullable=True)
