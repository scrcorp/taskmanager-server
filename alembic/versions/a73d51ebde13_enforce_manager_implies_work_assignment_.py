"""enforce manager implies work_assignment in user_stores

Revision ID: a73d51ebde13
Revises: 3164bc074b75
Create Date: 2026-05-19 11:46:39.027339

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a73d51ebde13'
down_revision: Union[str, None] = '3164bc074b75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 룰: is_manager=true 면 반드시 is_work_assignment=true (work 해제 불가).
    # 기존 데이터에 룰 위반 행이 있으면 정정.
    op.execute("""
        UPDATE user_stores
        SET is_work_assignment = TRUE
        WHERE is_manager = TRUE AND is_work_assignment = FALSE
    """)
    # DB 레벨 보장 — CHECK 제약: manager → work
    op.execute("""
        ALTER TABLE user_stores
        ADD CONSTRAINT ck_user_stores_manager_implies_work
        CHECK (NOT is_manager OR is_work_assignment)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE user_stores DROP CONSTRAINT IF EXISTS ck_user_stores_manager_implies_work")
