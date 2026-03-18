"""make schedule_requests.period_id nullable

Revision ID: t1u2v3w4x5y6
Revises: s1c2h3e4d5u6
Create Date: 2026-03-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t1u2v3w4x5y6"
down_revision: str = "s1c2h3e4d5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Make period_id nullable
    op.alter_column(
        "schedule_requests",
        "period_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )

    # Change FK ondelete from CASCADE to SET NULL
    op.drop_constraint(
        "schedule_requests_period_id_fkey",
        "schedule_requests",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "schedule_requests_period_id_fkey",
        "schedule_requests",
        "schedule_periods",
        ["period_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Revert FK ondelete back to CASCADE
    op.drop_constraint(
        "schedule_requests_period_id_fkey",
        "schedule_requests",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "schedule_requests_period_id_fkey",
        "schedule_requests",
        "schedule_periods",
        ["period_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Make period_id NOT NULL again
    op.alter_column(
        "schedule_requests",
        "period_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
