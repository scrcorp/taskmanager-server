"""model-b backfill: users->org_members, user_stores->org_member_stores, name parse, last_org_id, status

Revision ID: 77271e1d9753
Revises: f8020a1b7740
Create Date: 2026-07-02 16:59:28.180221

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77271e1d9753'
down_revision: Union[str, None] = 'f8020a1b7740'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) users → org_members (사람×org 소속 1행). org별 속성을 org_member 로 이동(복사).
    #    status: 삭제됨→terminated, 그 외 active. (계정 비활성은 users.status 에서 다룸)
    op.execute(
        """
        INSERT INTO org_members
            (id, user_id, organization_id, role_id, hourly_rate, department,
             clockin_pin, employee_no, status, created_at, updated_at)
        SELECT gen_random_uuid(), u.id, u.organization_id, u.role_id, u.hourly_rate,
               u.department, u.clockin_pin, u.employee_no,
               CASE WHEN u.deleted_at IS NOT NULL THEN 'terminated' ELSE 'active' END,
               u.created_at, u.updated_at
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM org_members m
            WHERE m.user_id = u.id AND m.organization_id = u.organization_id
        )
        """
    )

    # 2) user_stores → org_member_stores. 매장의 org 와 소속 org 를 매칭해 정확히 배선.
    op.execute(
        """
        INSERT INTO org_member_stores
            (id, org_member_id, store_id, is_manager, is_work_assignment, created_at)
        SELECT gen_random_uuid(), m.id, us.store_id, us.is_manager, us.is_work_assignment, us.created_at
        FROM user_stores us
        JOIN stores s ON s.id = us.store_id
        JOIN org_members m ON m.user_id = us.user_id AND m.organization_id = s.organization_id
        WHERE NOT EXISTS (
            SELECT 1 FROM org_member_stores oms
            WHERE oms.org_member_id = m.id AND oms.store_id = us.store_id
        )
        """
    )

    # 3) full_name → first/middle/last 파싱.
    #    규칙: 공백 기준 단어들 중 first=맨 앞, last=맨 뒤, middle=그 사이 전부.
    #    1단어=first만, 2단어=first+last(middle 없음), 3+단어=first+middle+last.
    op.execute(
        """
        UPDATE users u
        SET first_name = w.arr[1],
            last_name = CASE WHEN w.n >= 2 THEN w.arr[w.n] ELSE NULL END,
            middle_name = CASE WHEN w.n >= 3
                               THEN array_to_string(w.arr[2:w.n - 1], ' ')
                               ELSE NULL END
        FROM (
            SELECT id,
                   regexp_split_to_array(btrim(full_name), '\\s+') AS arr,
                   array_length(regexp_split_to_array(btrim(full_name), '\\s+'), 1) AS n
            FROM users
            WHERE full_name IS NOT NULL AND btrim(full_name) <> ''
        ) w
        WHERE u.id = w.id AND u.first_name IS NULL
        """
    )

    # 4) last_org_id 백필 = 기존 단일 org 소속.
    op.execute("UPDATE users SET last_org_id = organization_id WHERE last_org_id IS NULL")

    # 5) users.status 정교화 (schema 단계 server_default='active' → is_active/deleted 반영).
    op.execute(
        """
        UPDATE users
        SET status = CASE
            WHEN deleted_at IS NOT NULL THEN 'deleted'
            WHEN is_active THEN 'active'
            ELSE 'deactivated'
        END
        """
    )
    # 참고: platform_admins 시드는 하지 않음 — 어느 user 가 운영자인지는 데이터로 알 수 없음.
    # ENV break-glass 로 운영 후 백오피스에서 operator 부여(별도 단계).


def downgrade() -> None:
    op.execute("DELETE FROM org_member_stores")
    op.execute("DELETE FROM org_members")
    op.execute(
        "UPDATE users SET first_name = NULL, middle_name = NULL, last_name = NULL, "
        "last_org_id = NULL, status = 'active'"
    )
