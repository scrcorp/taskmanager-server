"""contacts: 구 컬럼 제거 — email / memo (마이그레이션 B = create-then-delete 의 delete)

A(c7a1b2d3e4f5) 에서 contact_emails / contacts.notes 로 옮겨 담았다. 코드가 새 필드만
쓰게 된 뒤에 이 리비전으로 구 컬럼을 지운다.

Revision ID: d8b2c3e4f5a6
Revises: c7a1b2d3e4f5
Create Date: 2026-08-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8b2c3e4f5a6"
down_revision: Union[str, None] = "c7a1b2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("contacts", "email")
    op.drop_column("contacts", "memo")


def downgrade() -> None:
    """되돌리면 컬럼을 다시 만들고 **자식/신 컬럼에서 값을 복구**한다.

    빈 컬럼만 만들어 놓으면 롤백 직후 이메일·메모가 통째로 사라진 것처럼 보인다.
    이메일이 여러 개면 대표(없으면 첫 번째) 하나만 복구된다 — 단일 컬럼이라 그 이상은 담기지 않는다.
    """
    op.add_column("contacts", sa.Column("email", sa.String(length=255), nullable=True))
    op.add_column("contacts", sa.Column("memo", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE contacts c
        SET email = e.address
        FROM (
            SELECT DISTINCT ON (contact_id) contact_id, address
            FROM contact_emails
            ORDER BY contact_id, is_primary DESC, sort_order
        ) e
        WHERE e.contact_id = c.id
        """
    )
    op.execute("UPDATE contacts SET memo = notes WHERE notes IS NOT NULL")
