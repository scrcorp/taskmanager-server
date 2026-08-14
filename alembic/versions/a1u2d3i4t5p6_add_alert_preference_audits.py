"""add alert_preference_audits

알림 수신 설정 변경 이력. "언제 껐는지" 를 시점 조회할 수 있어야 하므로
(카테고리, 채널) 단위 변화 1건 = 1행으로 남긴다.

Revision ID: a1u2d3i4t5p6
Revises: w1e2b3p4u5s6
Create Date: 2026-08-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1u2d3i4t5p6"
down_revision: str = "w1e2b3p4u5s6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_preference_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("category_code", sa.String(length=50), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        # 3-상태: True / False / NULL(미설정 = 기본값 따름)
        sa.Column("old_value", sa.Boolean(), nullable=True),
        sa.Column("new_value", sa.Boolean(), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["changed_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alert_pref_audits_lookup",
        "alert_preference_audits",
        ["user_id", "category_code", "channel", "changed_at"],
    )
    op.create_index(
        "ix_alert_pref_audits_organization_id",
        "alert_preference_audits",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_pref_audits_organization_id", table_name="alert_preference_audits")
    op.drop_index("ix_alert_pref_audits_lookup", table_name="alert_preference_audits")
    op.drop_table("alert_preference_audits")
