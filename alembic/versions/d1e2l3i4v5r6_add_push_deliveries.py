"""add push_deliveries

푸시 발송 시도 기록. "우리는 보냈다" 를 나중에 조회할 수 있어야 한다.
발송 1건 = 구독(기기) 1대 시도 = 1행.

Revision ID: d1e2l3i4v5r6
Revises: a1u2d3i4t5p6
Create Date: 2026-08-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2l3i4v5r6"
down_revision: str = "a1u2d3i4t5p6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # 알림이 지워져도 발송 사실은 남는다 → SET NULL
        sa.Column("alert_id", sa.Uuid(), nullable=True),
        sa.Column("alert_type", sa.String(length=50), nullable=True),
        # 구독 행이 정리돼도 어느 기기였는지 남도록 FK 가 아니라 값 스냅샷
        sa.Column("subscription_endpoint", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("skip_reason", sa.String(length=30), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("body", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_push_deliveries_user_created", "push_deliveries", ["user_id", "created_at"]
    )
    op.create_index("ix_push_deliveries_alert_id", "push_deliveries", ["alert_id"])
    op.create_index(
        "ix_push_deliveries_organization_id", "push_deliveries", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_push_deliveries_organization_id", table_name="push_deliveries")
    op.drop_index("ix_push_deliveries_alert_id", table_name="push_deliveries")
    op.drop_index("ix_push_deliveries_user_created", table_name="push_deliveries")
    op.drop_table("push_deliveries")
