"""promote first owner to super_owner remove auto-generated account

Revision ID: 3164bc074b75
Revises: 8148bc3b6509
Create Date: 2026-05-19 10:55:55.796531

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3164bc074b75'
down_revision: Union[str, None] = '8148bc3b6509'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: 자동 생성된 별도 super_owner 계정 제거.
    # 안전조건: full_name='Super Owner' + must_change_password=true (사용자가 손 안 댄 상태).
    # 사용자가 비밀번호 바꿨거나 이름 바꿨으면 사람이 쓰고 있는 거라 두자.
    op.execute("""
        DELETE FROM users u
        USING roles r
        WHERE u.role_id = r.id
          AND r.priority = 5
          AND u.full_name = 'Super Owner'
          AND u.must_change_password = TRUE
    """)

    # Step 2: 각 조직의 가장 먼저 생긴 owner(priority=10) 1명을 super_owner(priority=5) role 로 승격.
    # 다수 super_owner 가 이미 있는 조직은 그대로 두고 추가 승격만 차단.
    op.execute("""
        UPDATE users u
        SET role_id = sor.id
        FROM roles sor
        WHERE sor.organization_id = u.organization_id
          AND sor.priority = 5
          AND u.id = (
              SELECT u2.id FROM users u2
              JOIN roles r2 ON r2.id = u2.role_id
              WHERE u2.organization_id = u.organization_id
                AND r2.priority = 10
                AND u2.is_active = TRUE
                AND u2.deleted_at IS NULL
              ORDER BY u2.created_at ASC
              LIMIT 1
          )
          AND NOT EXISTS (
              SELECT 1 FROM users u3
              JOIN roles r3 ON r3.id = u3.role_id
              WHERE u3.organization_id = u.organization_id
                AND r3.priority = 5
                AND u3.deleted_at IS NULL
          )
    """)


def downgrade() -> None:
    # super_owner role 을 가진 user 들을 owner role 로 강등.
    # 자동 생성된 계정은 복구 안 함 (downgrade 시 한 번 더 8148bc3b6509 돌리면 복구됨).
    op.execute("""
        UPDATE users u
        SET role_id = or_.id
        FROM roles sor, roles or_
        WHERE u.role_id = sor.id
          AND sor.priority = 5
          AND or_.organization_id = sor.organization_id
          AND or_.priority = 10
    """)
