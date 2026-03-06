"""add timezone to organizations and stores

Revision ID: p1q2r3s4t5u6
Revises: c3d4e5f6g7h8
Create Date: 2026-03-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "p1q2r3s4t5u6"
down_revision: str = "c3d4e5f6g7h8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("timezone", sa.String(50), nullable=False, server_default="America/Los_Angeles"),
    )
    op.add_column(
        "stores",
        sa.Column("timezone", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stores", "timezone")
    op.drop_column("organizations", "timezone")
