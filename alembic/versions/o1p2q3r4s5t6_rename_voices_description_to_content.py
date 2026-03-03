"""rename_voices_description_to_content

Revision ID: o1p2q3r4s5t6
Revises: n1o2p3q4r5s6
Create Date: 2026-03-03
"""

from alembic import op

revision = "o1p2q3r4s5t6"
down_revision = "n1o2p3q4r5s6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("voices", "description", new_column_name="content")


def downgrade() -> None:
    op.alter_column("voices", "content", new_column_name="description")
