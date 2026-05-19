"""rename issues to tasks + drop legacy additional_tasks

Revision ID: c71d3411482f
Revises: 01b1c2e00db1
Create Date: 2026-05-18 18:14:05.004880

변경 사항:
- `issues` 테이블 → `tasks` 로 rename (데이터 보존)
- `issue_assignees` 테이블 → `task_assignees` 로 rename, FK 컬럼 `issue_id` → `task_id`,
  unique constraint 도 rename
- 인덱스 rename: ix_issues_* → ix_tasks_*, uq_issue_assignee → uq_task_assignee
- legacy `additional_tasks`, `additional_task_assignees`, `task_evidences` (communication 의 옛 추가업무 증빙), `issue_evidences` (이슈 시절 증빙) 모두 drop
- promote 시 사용했던 report.payload 의 `linked_issue_id` 키는 데이터 그대로 두고 어플 코드에서 양쪽 키를 모두 인식 (task_service 의 LINKED_TASK_KEYS 참조)

NOTE: 다른 unrelated drift (announcements / notifications / users.notification_preferences /
인덱스명 변경 등) 는 이 마이그레이션에서 다루지 않음.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c71d3411482f'
down_revision: Union[str, None] = '01b1c2e00db1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── legacy 정리 (additional_tasks / task_evidences / issue_evidences) ──
    op.execute("DROP TABLE IF EXISTS task_evidences CASCADE")
    op.execute("DROP TABLE IF EXISTS additional_task_assignees CASCADE")
    op.execute("DROP TABLE IF EXISTS additional_tasks CASCADE")
    op.execute("DROP TABLE IF EXISTS issue_evidences CASCADE")

    # ── issues → tasks rename ─────────────────────────────────────────
    op.rename_table('issues', 'tasks')

    # 인덱스 rename
    op.execute("ALTER INDEX IF EXISTS ix_issues_source_report_id RENAME TO ix_tasks_source_report_id")
    op.execute("ALTER INDEX IF EXISTS ix_issues_org_status RENAME TO ix_tasks_org_status")

    # PK / FK constraint 이름은 sqlalchemy 가 자동 추론하므로 그대로 둠 (Postgres 는 constraint name 만 string 비교).
    # 다만 명시적 FK constraint name 들 (issues_*_fkey) 은 그대로 둬도 동작에 영향 없음. cosmetic 만 다음 정리에서.

    # ── issue_assignees → task_assignees rename ───────────────────────
    op.rename_table('issue_assignees', 'task_assignees')
    op.alter_column('task_assignees', 'issue_id', new_column_name='task_id')
    op.execute("ALTER TABLE task_assignees RENAME CONSTRAINT uq_issue_assignee TO uq_task_assignee")

    # ── permissions: issues:* → tasks:* 매핑 이전 ─────────────────────
    # role_permissions.permission_id 는 CASCADE 라서 issues:* permission row 를
    # 그냥 지우면 role 매핑도 같이 사라짐. 미리 tasks:* 로 row 추가 후 issues:* 삭제.
    op.execute(
        """
        INSERT INTO role_permissions (id, role_id, permission_id, created_at)
        SELECT gen_random_uuid(), rp.role_id, pnew.id, now()
        FROM role_permissions rp
        JOIN permissions pold ON pold.id = rp.permission_id
        JOIN permissions pnew ON pnew.code = REPLACE(pold.code, 'issues:', 'tasks:')
        WHERE pold.code LIKE 'issues:%'
          AND pnew.code LIKE 'tasks:%'
        ON CONFLICT (role_id, permission_id) DO NOTHING
        """
    )
    op.execute("DELETE FROM permissions WHERE code LIKE 'issues:%'")


def downgrade() -> None:
    # ── task_assignees → issue_assignees ──────────────────────────────
    op.execute("ALTER TABLE task_assignees RENAME CONSTRAINT uq_task_assignee TO uq_issue_assignee")
    op.alter_column('task_assignees', 'task_id', new_column_name='issue_id')
    op.rename_table('task_assignees', 'issue_assignees')

    # ── tasks → issues ────────────────────────────────────────────────
    op.execute("ALTER INDEX IF EXISTS ix_tasks_org_status RENAME TO ix_issues_org_status")
    op.execute("ALTER INDEX IF EXISTS ix_tasks_source_report_id RENAME TO ix_issues_source_report_id")
    op.rename_table('tasks', 'issues')

    # legacy 테이블 복원은 의미 없음 (데이터 없음) — no-op.
