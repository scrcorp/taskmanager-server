"""contacts: 링크에도 대표(is_primary) — 메인 연락수단은 채널을 가로질러 **딱 하나**다

전화/이메일에만 대표가 있으면 "링크가 메인인 업체"를 표현할 수 없다(전화 없이 주문 포털만
쓰는 곳이 실제로 있다). 대표 플래그를 세 채널에 모두 두고, **연락처당 단 하나만 true** 인 것은
서비스가 보장한다(테이블을 가로지르는 제약이라 DB 제약으로는 표현할 수 없다).

Revision ID: e9c3d4f5a6b7
Revises: d8b2c3e4f5a6
Create Date: 2026-08-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9c3d4f5a6b7"
down_revision: Union[str, None] = "d8b2c3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contact_links",
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # 기존 데이터 정리 — 전화/이메일에 대표가 여럿일 수 있다(예전에는 채널별로 하나였다).
    # 채널을 가로질러 하나만 남긴다: 전화 > 이메일 > 링크 순, 각 채널에서는 sort_order 순.
    op.execute(
        """
        WITH keep AS (
            SELECT DISTINCT ON (contact_id) contact_id, kind, id
            FROM (
                SELECT contact_id, 'phone' AS kind, id, sort_order, 1 AS rank
                FROM contact_phones WHERE is_primary
                UNION ALL
                SELECT contact_id, 'email', id, sort_order, 2 FROM contact_emails WHERE is_primary
            ) t
            ORDER BY contact_id, rank, sort_order
        )
        UPDATE contact_phones p SET is_primary = false
        WHERE p.is_primary
          AND NOT EXISTS (
              SELECT 1 FROM keep k
              WHERE k.contact_id = p.contact_id AND k.kind = 'phone' AND k.id = p.id
          )
        """
    )
    op.execute(
        """
        WITH keep AS (
            SELECT DISTINCT ON (contact_id) contact_id, kind, id
            FROM (
                SELECT contact_id, 'phone' AS kind, id, sort_order, 1 AS rank
                FROM contact_phones WHERE is_primary
                UNION ALL
                SELECT contact_id, 'email', id, sort_order, 2 FROM contact_emails WHERE is_primary
            ) t
            ORDER BY contact_id, rank, sort_order
        )
        UPDATE contact_emails e SET is_primary = false
        WHERE e.is_primary
          AND NOT EXISTS (
              SELECT 1 FROM keep k
              WHERE k.contact_id = e.contact_id AND k.kind = 'email' AND k.id = e.id
          )
        """
    )


def downgrade() -> None:
    op.drop_column("contact_links", "is_primary")
