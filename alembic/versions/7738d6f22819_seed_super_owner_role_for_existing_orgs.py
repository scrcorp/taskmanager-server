"""seed super_owner role for existing orgs

Revision ID: 7738d6f22819
Revises: 0ecfa8fac48e
Create Date: 2026-05-18 19:10:50.189418

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7738d6f22819'
down_revision: Union[str, None] = '0ecfa8fac48e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 기존 모든 organization 에 super_owner role 생성 (priority=5).
    # role_permissions 시드는 server startup 의 sync_default_role_permissions
    # hook 이 자동 처리. 본 migration 은 role row 만 보장.
    # 이미 super_owner role 이 존재하는 조직은 skip (재실행 안전).
    op.execute("""
        INSERT INTO roles (id, organization_id, name, priority, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            o.id,
            'super_owner',
            5,
            now(),
            now()
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM roles r
            WHERE r.organization_id = o.id
              AND r.name = 'super_owner'
        )
    """)


def downgrade() -> None:
    # super_owner role 제거. role_permissions 는 FK CASCADE 로 정리됨.
    # super_owner role 에 user 가 할당돼 있으면 실패 (의도된 안전장치).
    op.execute("DELETE FROM roles WHERE name = 'super_owner'")
