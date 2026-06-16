"""store code partial unique index

Revision ID: 026dd7a0ad8f
Revises: 9af27ba09f1a
Create Date: 2026-06-16 09:04:41.192761

stores.code 는 org 내 유일해야 한다(파일명 식별자 충돌 방지).
컬럼은 이미 존재(String(10), nullable) → unique index 만 추가.
partial: code NULL(미부여) 다수 허용 + soft-delete 된 스토어는 코드 반납.

NOTE: autogenerate 가 무관 인덱스 drop 을 오검출하여 제거하고 의도한 변경만 남겼다.
      실제 8개 스토어의 code 값 backfill 은 별도 idempotent 스크립트
      (scripts/backfill_store_codes.py) 로 처리한다(데이터 작업 분리).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '026dd7a0ad8f'
down_revision: Union[str, None] = '9af27ba09f1a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'uq_store_org_code',
        'stores',
        ['organization_id', 'code'],
        unique=True,
        postgresql_where=sa.text('code IS NOT NULL AND deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_store_org_code',
        table_name='stores',
        postgresql_where=sa.text('code IS NOT NULL AND deleted_at IS NULL'),
    )
