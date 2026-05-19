"""auto create super_owner account for existing orgs

Revision ID: 8148bc3b6509
Revises: ad7a5df5aed1
Create Date: 2026-05-18 23:22:33.776551

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.utils.password import hash_password


# revision identifiers, used by Alembic.
revision: str = '8148bc3b6509'
down_revision: Union[str, None] = 'ad7a5df5aed1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 기존 모든 조직에 Super Owner 계정 자동 생성.
    # username = organization.name (회사명 그대로), password = "1234"
    # must_change_password=true → Console 첫 로그인 시 강제 변경.
    # 이미 super_owner 계정이 있는 조직은 skip.
    conn = op.get_bind()
    conn.execute(
        text("""
            INSERT INTO users (
                id, organization_id, role_id, username, full_name,
                password_hash, must_change_password, is_active, email_verified,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                o.id,
                r.id,
                o.name,
                'Super Owner',
                :pw_hash,
                TRUE,
                TRUE,
                TRUE,
                now(),
                now()
            FROM organizations o
            JOIN roles r ON r.organization_id = o.id AND r.name = 'super_owner'
            WHERE NOT EXISTS (
                SELECT 1 FROM users u
                WHERE u.organization_id = o.id AND u.role_id = r.id
            )
        """),
        {"pw_hash": hash_password("1234")},
    )


def downgrade() -> None:
    # super_owner role 을 가진 모든 user 제거. role 자체는 별 migration 에서 처리.
    op.execute("""
        DELETE FROM users
        WHERE role_id IN (SELECT id FROM roles WHERE name = 'super_owner')
    """)
