"""add schedule system tables

Revision ID: s1c2h3e4d5u6
Revises: p1q2r3s4t5u6
Create Date: 2026-03-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s1c2h3e4d5u6"
down_revision: str = "p1q2r3s4t5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. store_work_roles
    op.create_table(
        "store_work_roles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shift_id", sa.Uuid(), sa.ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position_id", sa.Uuid(), sa.ForeignKey("positions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("default_start_time", sa.Time(), nullable=True),
        sa.Column("default_end_time", sa.Time(), nullable=True),
        sa.Column("break_start_time", sa.Time(), nullable=True),
        sa.Column("break_end_time", sa.Time(), nullable=True),
        sa.Column("required_headcount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("default_checklist_id", sa.Uuid(), sa.ForeignKey("checklist_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("store_id", "shift_id", "position_id", name="uq_store_work_role"),
    )
    op.create_index("ix_store_work_roles_store", "store_work_roles", ["store_id"])

    # 2. store_break_rules
    op.create_table(
        "store_break_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("max_continuous_minutes", sa.Integer(), nullable=False, server_default="240"),
        sa.Column("break_duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_daily_work_minutes", sa.Integer(), nullable=False, server_default="480"),
        sa.Column("work_hour_calc_basis", sa.String(20), nullable=False, server_default="per_store"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # 3. schedule_periods
    op.create_table(
        "schedule_periods",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("request_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_schedule_periods_org_store", "schedule_periods", ["organization_id", "store_id"])
    op.create_index("ix_schedule_periods_dates", "schedule_periods", ["store_id", "period_start", "period_end"])

    # 4. schedule_request_templates
    op.create_table(
        "schedule_request_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_request_templates_user_store", "schedule_request_templates", ["user_id", "store_id"])

    # 5. schedule_request_template_items
    op.create_table(
        "schedule_request_template_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("template_id", sa.Uuid(), sa.ForeignKey("schedule_request_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("work_role_id", sa.Uuid(), sa.ForeignKey("store_work_roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("preferred_start_time", sa.Time(), nullable=True),
        sa.Column("preferred_end_time", sa.Time(), nullable=True),
        sa.UniqueConstraint("template_id", "day_of_week", "work_role_id", name="uq_template_day_role"),
    )

    # 6. schedule_requests
    op.create_table(
        "schedule_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("period_id", sa.Uuid(), sa.ForeignKey("schedule_periods.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("work_role_id", sa.Uuid(), sa.ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("preferred_start_time", sa.Time(), nullable=True),
        sa.Column("preferred_end_time", sa.Time(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitted"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_schedule_requests_period", "schedule_requests", ["period_id"])
    op.create_index("ix_schedule_requests_user_date", "schedule_requests", ["user_id", "work_date"])
    op.create_index("ix_schedule_requests_store", "schedule_requests", ["store_id"])

    # 7. schedule_entries
    op.create_table(
        "schedule_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_id", sa.Uuid(), sa.ForeignKey("schedule_periods.id", ondelete="SET NULL"), nullable=True),
        sa.Column("request_id", sa.Uuid(), sa.ForeignKey("schedule_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("work_role_id", sa.Uuid(), sa.ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("break_start_time", sa.Time(), nullable=True),
        sa.Column("break_end_time", sa.Time(), nullable=True),
        sa.Column("net_work_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("work_assignment_id", sa.Uuid(), sa.ForeignKey("work_assignments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_schedule_entries_org_store_date", "schedule_entries", ["organization_id", "store_id", "work_date"])
    op.create_index("ix_schedule_entries_user_date", "schedule_entries", ["user_id", "work_date"])
    op.create_index("ix_schedule_entries_period", "schedule_entries", ["period_id"])


def downgrade() -> None:
    op.drop_table("schedule_entries")
    op.drop_table("schedule_requests")
    op.drop_table("schedule_request_template_items")
    op.drop_table("schedule_request_templates")
    op.drop_table("schedule_periods")
    op.drop_table("store_break_rules")
    op.drop_table("store_work_roles")
