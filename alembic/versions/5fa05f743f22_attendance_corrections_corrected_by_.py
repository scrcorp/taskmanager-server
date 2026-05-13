"""attendance_corrections corrected_by nullable

System actor (cron auto clock-out) 도 correction 을 기록할 수 있도록 corrected_by 를
nullable 로 변경. 기존 FK ondelete=SET NULL 이 이미 NULL 을 허용하는 의도였으나
컬럼 자체가 NOT NULL 이라 system 액션을 기록할 수 없었음.

Revision ID: 5fa05f743f22
Revises: fcacb12802f7
Create Date: 2026-05-13 11:31:46.931398

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5fa05f743f22"
down_revision: Union[str, None] = "fcacb12802f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "attendance_corrections",
        "corrected_by",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "attendance_corrections",
        "corrected_by",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=False,
    )
