"""add_issue_reports

Revision ID: g1h2i3j4k5l6
Revises: d2e3f4a5b6c7
Create Date: 2026-02-25 10:00:00.000000

이슈 리포트(issue_reports) 테이블 생성.
전 역할 작성 가능한 이슈 보고 기능.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "issue_reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", UUID(as_uuid=True), sa.ForeignKey("stores.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(30), server_default="other", nullable=False),
        sa.Column("status", sa.String(20), server_default="open", nullable=False),
        sa.Column("priority", sa.String(20), server_default="normal", nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("resolved_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_issue_reports_org_id", "issue_reports", ["organization_id"])
    op.create_index("ix_issue_reports_status", "issue_reports", ["status"])
    op.create_index("ix_issue_reports_created_by", "issue_reports", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_issue_reports_created_by")
    op.drop_index("ix_issue_reports_status")
    op.drop_index("ix_issue_reports_org_id")
    op.drop_table("issue_reports")
