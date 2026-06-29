"""store: status enum (replace is_active), phone, email, sort_order

Revision ID: a82888275180
Revises: a4673e5f2cb1
Create Date: 2026-06-24 18:26:54.729883

매장 라이프사이클 status(preparing/open/paused/closed)로 is_active 컬럼 대체 +
연락 필드(phone/email) + 수동 정렬(sort_order) 추가.

NOTE: autogenerate 가 모델↔DB 사전 드리프트(notifications/announcements 등)를 함께
잡았으나 본 마이그레이션은 stores 변경만 다룬다 (그 드리프트는 별도 이슈).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a82888275180'
down_revision: Union[str, None] = 'a4673e5f2cb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 신규 컬럼 추가
    op.add_column('stores', sa.Column('phone', sa.String(length=30), nullable=True))
    op.add_column('stores', sa.Column('email', sa.String(length=255), nullable=True))
    op.add_column('stores', sa.Column('status', sa.String(length=20), server_default='open', nullable=False))
    op.add_column('stores', sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False))

    # 2) 기존 is_active → status 백필 (true→open, false→paused).
    #    이미 soft-delete(deleted_at) 된 행은 closed 로.
    op.execute(
        "UPDATE stores SET status = CASE "
        "WHEN deleted_at IS NOT NULL THEN 'closed' "
        "WHEN is_active THEN 'open' "
        "ELSE 'paused' END"
    )

    # 3) sort_order 초기값을 org 내 created_at 순서로 부여 (0..N).
    op.execute(
        "UPDATE stores s SET sort_order = sub.rn FROM ("
        "SELECT id, (row_number() OVER (PARTITION BY organization_id ORDER BY created_at) - 1) AS rn "
        "FROM stores) sub WHERE s.id = sub.id"
    )

    # 4) 구 is_active 컬럼 제거 (status 가 SoT, 모델은 hybrid_property 로 파생)
    op.drop_column('stores', 'is_active')


def downgrade() -> None:
    op.add_column('stores', sa.Column('is_active', sa.BOOLEAN(), server_default=sa.text('true'), nullable=False))
    # status → is_active 복원 (open→true, 그 외→false)
    op.execute("UPDATE stores SET is_active = (status = 'open')")
    op.drop_column('stores', 'sort_order')
    op.drop_column('stores', 'status')
    op.drop_column('stores', 'email')
    op.drop_column('stores', 'phone')
