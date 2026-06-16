"""warning ordinal_snapshot — 직원별 차수 스냅샷 + partial unique

Revision ID: 9af27ba09f1a
Revises: 977ffdc0bc60
Create Date: 2026-06-15 18:06:05.860145

직원별 "N차 경고" 를 발행 시점에 불변 스냅샷으로 고정한다.
- ordinal_snapshot: 같은 직원의 발행순서(1-based). 철회/복구로 변하지 않음(서류 무결성).
- backfill: 기존 경고를 (created_at, seq) 순으로 직원별 1..K 부여(soft-delete 제외, 철회 포함).
- partial unique: 동시 발행 race 직렬화. NULL(=직원 삭제/legacy 미부여)은 다수 허용.

NOTE: autogenerate 가 announcements/notifications 등 무관 테이블 drop 을 오검출하여
      제거하고 의도한 변경만 남겼다(이 repo 의 알려진 autogenerate 노이즈).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9af27ba09f1a'
down_revision: Union[str, None] = '977ffdc0bc60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 컬럼 추가 (nullable — 기존 행은 backfill 로 채운다)
    op.add_column('warnings', sa.Column('ordinal_snapshot', sa.Integer(), nullable=True))

    # 2) 기존 경고 backfill — 직원별 (created_at, seq) 발행순서로 1..K.
    #    soft-delete(deleted_at) 제외, 철회(status=withdrawn) 포함, subject NULL 제외.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY organization_id, subject_user_id
                       ORDER BY created_at, seq
                   ) AS rn
            FROM warnings
            WHERE deleted_at IS NULL
              AND subject_user_id IS NOT NULL
        )
        UPDATE warnings w
        SET ordinal_snapshot = ranked.rn
        FROM ranked
        WHERE w.id = ranked.id
        """
    )

    # 3) partial unique index (backfill 후 생성 → 데이터 정합 검증 효과)
    op.create_index(
        'uq_warning_subject_ordinal',
        'warnings',
        ['organization_id', 'subject_user_id', 'ordinal_snapshot'],
        unique=True,
        postgresql_where=sa.text('ordinal_snapshot IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_warning_subject_ordinal',
        table_name='warnings',
        postgresql_where=sa.text('ordinal_snapshot IS NOT NULL'),
    )
    op.drop_column('warnings', 'ordinal_snapshot')
