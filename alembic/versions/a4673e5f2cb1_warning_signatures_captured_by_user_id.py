"""warning_signatures captured_by_user_id

Revision ID: a4673e5f2cb1
Revises: 71b3ba391288
Create Date: 2026-06-19 10:40:11.886330

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4673e5f2cb1'
down_revision: Union[str, None] = '71b3ba391288'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FK_NAME = "fk_warning_signatures_captured_by_user_id"


def upgrade() -> None:
    # warning_signatures.captured_by_user_id — 온-디바이스 캡처 시 실제 조작 계정(감사).
    # nullable, users(id) SET NULL. (autogenerate 가 함께 감지한 무관 drift 는 제거함.)
    op.add_column(
        "warning_signatures",
        sa.Column("captured_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        FK_NAME,
        "warning_signatures",
        "users",
        ["captured_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(FK_NAME, "warning_signatures", type_="foreignkey")
    op.drop_column("warning_signatures", "captured_by_user_id")
