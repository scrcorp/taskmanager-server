"""add push_subscriptions (web push)

웹 푸시 구독(기기) 저장 테이블. 한 사용자가 여러 기기를 가질 수 있으므로
user_id 는 중복 허용, endpoint 는 기기 식별자라 UNIQUE.

Revision ID: w1e2b3p4u5s6
Revises: 22390ed69baa
Create Date: 2026-08-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "w1e2b3p4u5s6"
down_revision: str = "22390ed69baa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # 푸시 중계 URL — 길이가 가변적이고 길어서 Text
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=255), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # 같은 endpoint 가 두 행이면 같은 기기에 알림이 두 번 간다.
        sa.UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )
    # 발송 시 "이 사용자의 모든 기기" 조회가 가장 잦다.
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])
    op.create_index(
        "ix_push_subscriptions_organization_id", "push_subscriptions", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_push_subscriptions_organization_id", table_name="push_subscriptions")
    op.drop_index("ix_push_subscriptions_user_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
