"""drop attendance_devices.revoked_at + delete revoked rows

기기 revoke 동작을 hard delete 로 전환. 기존 revoked_at IS NOT NULL row 는
정리하고 컬럼 자체와 관련 index 를 제거한다.

Revision ID: fcacb12802f7
Revises: ff7a90699902
Create Date: 2026-05-11 16:13:26.868866

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fcacb12802f7'
down_revision: Union[str, None] = 'ff7a90699902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM attendance_devices WHERE revoked_at IS NOT NULL")
    op.drop_index(
        "ix_attendance_devices_org_active", table_name="attendance_devices"
    )
    op.drop_column("attendance_devices", "revoked_at")
    op.create_index(
        "ix_attendance_devices_org",
        "attendance_devices",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_attendance_devices_org", table_name="attendance_devices")
    op.add_column(
        "attendance_devices",
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_attendance_devices_org_active",
        "attendance_devices",
        ["organization_id", "revoked_at"],
    )
