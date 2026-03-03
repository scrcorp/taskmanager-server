"""rename_issue_reports_to_voices

Revision ID: n1o2p3q4r5s6
Revises: m1n2o3p4q5r6
Create Date: 2026-03-03
"""

from alembic import op

revision = "n1o2p3q4r5s6"
down_revision = "m1n2o3p4q5r6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("issue_reports", "voices")

    # Rename indexes (PostgreSQL naming convention: ix_tablename_column)
    op.execute("ALTER INDEX IF EXISTS ix_issue_reports_organization_id RENAME TO ix_voices_organization_id")
    op.execute("ALTER INDEX IF EXISTS ix_issue_reports_store_id RENAME TO ix_voices_store_id")
    op.execute("ALTER INDEX IF EXISTS ix_issue_reports_created_by RENAME TO ix_voices_created_by")
    op.execute("ALTER INDEX IF EXISTS ix_issue_reports_status RENAME TO ix_voices_status")
    op.execute("ALTER INDEX IF EXISTS ix_issue_reports_category RENAME TO ix_voices_category")


def downgrade() -> None:
    op.rename_table("voices", "issue_reports")

    op.execute("ALTER INDEX IF EXISTS ix_voices_organization_id RENAME TO ix_issue_reports_organization_id")
    op.execute("ALTER INDEX IF EXISTS ix_voices_store_id RENAME TO ix_issue_reports_store_id")
    op.execute("ALTER INDEX IF EXISTS ix_voices_created_by RENAME TO ix_issue_reports_created_by")
    op.execute("ALTER INDEX IF EXISTS ix_voices_status RENAME TO ix_issue_reports_status")
    op.execute("ALTER INDEX IF EXISTS ix_voices_category RENAME TO ix_issue_reports_category")
