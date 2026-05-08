"""drop sv checklists:read permission

Revision ID: 6f966454bea8
Revises: ee2d6feb662e
Create Date: 2026-05-08 17:58:32.893560

SV 역할(priority=30)에서 checklists:read 권한을 제거한다.
DEFAULT_ROLE_PERMISSIONS에서는 이미 제거됐지만 sync 함수가 INSERT-only라
기존 조직의 SV 역할에 잔존한 권한을 정리하기 위한 data migration.

Templates는 SV에게 노출하지 않고, 검토(progress)는 checklist_review:read로
분리됨에 따라 더 이상 필요 없음.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '6f966454bea8'
down_revision: Union[str, None] = 'ee2d6feb662e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE role_id IN (SELECT id FROM roles WHERE priority = 30)
          AND permission_id = (
            SELECT id FROM permissions WHERE code = 'checklists:read'
          );
        """
    )


def downgrade() -> None:
    # 모든 priority=30 역할에 checklists:read를 다시 부여 (이미 있으면 skip).
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r CROSS JOIN permissions p
        WHERE r.priority = 30
          AND p.code = 'checklists:read'
          AND NOT EXISTS (
            SELECT 1 FROM role_permissions rp
            WHERE rp.role_id = r.id AND rp.permission_id = p.id
          );
        """
    )
