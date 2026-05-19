"""backfill additional_tasks into issues

Revision ID: fb3480f91eb1
Revises: 8c92c52c2c67
Create Date: 2026-05-11 16:40:52.950390

기존 additional_tasks / additional_task_assignees / task_evidences →
issues / issue_assignees / issue_evidences로 복사 (ID 보존, 멱등).
source_report_id는 NULL (기존 task는 신고에서 promote된 게 아님).
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fb3480f91eb1'
down_revision: Union[str, None] = '8c92c52c2c67'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. issues
    op.execute("""
        INSERT INTO issues (
            id, organization_id, store_id, title, description,
            priority, status, due_date, created_by, source_report_id,
            deleted_at, created_at, updated_at
        )
        SELECT
            id, organization_id, store_id, title, description,
            priority, status, due_date, created_by, NULL,
            deleted_at, created_at, updated_at
        FROM additional_tasks
        ON CONFLICT (id) DO NOTHING
    """)

    # 2. issue_assignees
    op.execute("""
        INSERT INTO issue_assignees (id, issue_id, user_id, created_at)
        SELECT id, task_id, user_id, created_at
        FROM additional_task_assignees
        ON CONFLICT (id) DO NOTHING
    """)

    # 3. issue_evidences (task_id → issue_id 매핑)
    op.execute("""
        INSERT INTO issue_evidences (id, issue_id, user_id, file_url, file_type, note, created_at)
        SELECT id, task_id, user_id, file_url, file_type, note, created_at
        FROM task_evidences
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM issue_evidences WHERE id IN (SELECT id FROM task_evidences)")
    op.execute("DELETE FROM issue_assignees WHERE id IN (SELECT id FROM additional_task_assignees)")
    op.execute("DELETE FROM issues WHERE id IN (SELECT id FROM additional_tasks)")
