"""grant console:access to existing non-staff roles (D12)

Revision ID: 261609d1aeae
Revises: 1d3a46e5711b
Create Date: 2026-08-14 10:46:03.319333

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '261609d1aeae'
down_revision: Union[str, None] = '1d3a46e5711b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    """console:access 권한 행을 만들고, 기존 non-staff role 전부에 부여한다.

    D12 로 콘솔 진입 게이트를 priority 비교에서 permission 으로 바꾼다. 백필이 없으면
    배포 순간 모든 관리자가 로그인할 수 없게 되므로, **게이트 전환과 같은 배포에 반드시
    함께 들어가야 한다.**

    부여 대상 = priority < 40(STAFF) 인 기존 role — 즉 전환 전과 동일한 집합이다.
    따라서 이 마이그레이션 직후 동작은 전환 전과 완전히 같고, 이후부터 개별 조정이 가능해진다.
    """
    op.execute(
        """
        INSERT INTO permissions (id, code, resource, action, description, require_priority_check, created_at)
        SELECT gen_random_uuid(), 'console:access', 'console', 'access',
               'Sign in to the admin console', false, now()
        WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code = 'console:access')
        """
    )
    op.execute(
        """
        INSERT INTO role_permissions (id, role_id, permission_id, created_at)
        SELECT gen_random_uuid(), r.id, p.id, now()
        FROM roles r
        CROSS JOIN permissions p
        WHERE p.code = 'console:access'
          AND r.priority < 40
          AND NOT EXISTS (
              SELECT 1 FROM role_permissions rp
              WHERE rp.role_id = r.id AND rp.permission_id = p.id
          )
        """
    )


def downgrade() -> None:
    """부여만 되돌린다. permissions 행은 registry 동기화가 다시 만들므로 두어도 무해하다."""
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE permission_id IN (SELECT id FROM permissions WHERE code = 'console:access')
        """
    )
