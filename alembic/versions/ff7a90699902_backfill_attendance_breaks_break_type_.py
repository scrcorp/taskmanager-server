"""backfill attendance_breaks break_type to paid_10min/unpaid_meal

기존 paid_short/unpaid_long 행을 신규 정식 값(paid_10min/unpaid_meal)으로 변환.
Phase 1 dual-read 단계에서 동일 PR 로 배포되어 코드가 신규 값으로 통일된 후에도
DB 잔존값을 깨끗하게 정리한다.

Revision ID: ff7a90699902
Revises: 7130b0a3da7a
Create Date: 2026-05-11 15:37:12.024825

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ff7a90699902'
down_revision: Union[str, None] = '7130b0a3da7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE attendance_breaks SET break_type = 'paid_10min' "
        "WHERE break_type = 'paid_short'"
    )
    op.execute(
        "UPDATE attendance_breaks SET break_type = 'unpaid_meal' "
        "WHERE break_type = 'unpaid_long'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE attendance_breaks SET break_type = 'paid_short' "
        "WHERE break_type = 'paid_10min'"
    )
    op.execute(
        "UPDATE attendance_breaks SET break_type = 'unpaid_long' "
        "WHERE break_type = 'unpaid_meal'"
    )
