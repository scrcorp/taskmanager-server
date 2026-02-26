"""rename_audit_log_to_completion_log

Revision ID: j1k2l3m4n5o6
Revises: i1j2k3l4m5n6
Create Date: 2026-02-26 16:00:00.000000

Rename permission code: audit_log:read → completion_log:read
"""

from alembic import op

revision = "j1k2l3m4n5o6"
down_revision = "i1j2k3l4m5n6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE permissions SET code = 'completion_log:read', resource = 'completion_log', description = 'Completion log read' "
        "WHERE code = 'audit_log:read'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE permissions SET code = 'audit_log:read', resource = 'audit_log', description = '감사 로그 조회' "
        "WHERE code = 'completion_log:read'"
    )
