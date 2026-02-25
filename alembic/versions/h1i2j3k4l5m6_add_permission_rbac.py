"""add_permission_rbac

Revision ID: h1i2j3k4l5m6
Revises: g1h2i3j4k5l6
Create Date: 2026-02-25 14:00:00.000000

Permission-Based RBAC 전환:
- roles.level → roles.priority 컬럼명 변경
- permissions 테이블 생성 (34개 seed)
- role_permissions 테이블 생성
- 기존 역할에 기본 permission 할당
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "h1i2j3k4l5m6"
down_revision: Union[str, None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 34개 permission 정의
PERMISSIONS = [
    ("stores:create", "stores", "create", "매장 생성", False),
    ("stores:read", "stores", "read", "매장 조회", False),
    ("stores:update", "stores", "update", "매장 수정", False),
    ("stores:delete", "stores", "delete", "매장 삭제", False),
    ("users:create", "users", "create", "사용자 생성", True),
    ("users:read", "users", "read", "사용자 조회", False),
    ("users:update", "users", "update", "사용자 수정", True),
    ("users:delete", "users", "delete", "사용자 삭제", True),
    ("roles:create", "roles", "create", "역할 생성", True),
    ("roles:read", "roles", "read", "역할 조회", False),
    ("roles:update", "roles", "update", "역할 수정", True),
    ("roles:delete", "roles", "delete", "역할 삭제", True),
    ("evaluations:create", "evaluations", "create", "평가 생성", False),
    ("evaluations:read", "evaluations", "read", "평가 조회", False),
    ("evaluations:update", "evaluations", "update", "평가 수정", False),
    ("evaluations:delete", "evaluations", "delete", "평가 삭제", False),
    ("schedules:create", "schedules", "create", "스케줄 생성", False),
    ("schedules:read", "schedules", "read", "스케줄 조회", False),
    ("schedules:update", "schedules", "update", "스케줄 수정", False),
    ("schedules:delete", "schedules", "delete", "스케줄 삭제", False),
    ("announcements:create", "announcements", "create", "공지 생성", False),
    ("announcements:read", "announcements", "read", "공지 조회", False),
    ("announcements:update", "announcements", "update", "공지 수정", False),
    ("announcements:delete", "announcements", "delete", "공지 삭제", False),
    ("checklists:create", "checklists", "create", "체크리스트 생성", False),
    ("checklists:read", "checklists", "read", "체크리스트 조회", False),
    ("checklists:update", "checklists", "update", "체크리스트 수정", False),
    ("checklists:delete", "checklists", "delete", "체크리스트 삭제", False),
    ("tasks:create", "tasks", "create", "업무 생성", False),
    ("tasks:read", "tasks", "read", "업무 조회", False),
    ("tasks:update", "tasks", "update", "업무 수정", False),
    ("tasks:delete", "tasks", "delete", "업무 삭제", False),
    ("dashboard:read", "dashboard", "read", "대시보드 조회", False),
    ("audit_log:read", "audit_log", "read", "감사 로그 조회", False),
]

# 역할별 기본 permission (priority 기반)
# Owner(10): 전체 34개
# GM(20): stores:create, stores:delete, roles:create, roles:delete 제외 = 30개
# SV(30): read 위주 + schedules:create = 10개
# Staff(40): 0개
GM_EXCLUDED = {"stores:create", "stores:delete", "roles:create", "roles:delete"}
SV_ALLOWED = {
    "stores:read", "users:read", "roles:read",
    "schedules:read", "schedules:create",
    "announcements:read", "checklists:read", "tasks:read",
    "evaluations:read", "dashboard:read",
}


def upgrade() -> None:
    # 1. roles.level → roles.priority rename
    op.alter_column("roles", "level", new_column_name="priority")

    # unique constraint rename
    op.drop_constraint("uq_role_org_level", "roles", type_="unique")
    op.create_unique_constraint("uq_role_org_priority", "roles", ["organization_id", "priority"])

    # 2. permissions 테이블 생성
    op.create_table(
        "permissions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("code", sa.String(100), unique=True, nullable=False),
        sa.Column("resource", sa.String(50), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("require_priority_check", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_permissions_resource_action", "permissions", ["resource", "action"], unique=True)

    # 3. role_permissions 테이블 생성
    op.create_table(
        "role_permissions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("role_id", UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission_id", UUID(as_uuid=True), sa.ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )
    op.create_index("idx_role_permissions_role_id", "role_permissions", ["role_id"])

    # 4. Permission seed 데이터 삽입
    conn = op.get_bind()

    # permissions INSERT
    for code, resource, action, description, require_priority_check in PERMISSIONS:
        conn.execute(
            sa.text(
                "INSERT INTO permissions (code, resource, action, description, require_priority_check) "
                "VALUES (:code, :resource, :action, :description, :rpc)"
            ),
            {"code": code, "resource": resource, "action": action, "description": description, "rpc": require_priority_check},
        )

    # 5. 기존 역할에 기본 permission 할당
    # 모든 permission id를 code 기준으로 조회
    perm_rows = conn.execute(sa.text("SELECT id, code FROM permissions")).fetchall()
    perm_map = {row[1]: row[0] for row in perm_rows}  # code -> id

    # 모든 기존 roles 조회
    role_rows = conn.execute(sa.text("SELECT id, priority FROM roles")).fetchall()

    for role_id, priority in role_rows:
        if priority <= 10:
            # Owner: 전체
            codes = [p[0] for p in PERMISSIONS]
        elif priority <= 20:
            # GM: 4개 제외
            codes = [p[0] for p in PERMISSIONS if p[0] not in GM_EXCLUDED]
        elif priority <= 30:
            # SV: 10개만
            codes = [p[0] for p in PERMISSIONS if p[0] in SV_ALLOWED]
        else:
            # Staff: 없음
            codes = []

        for code in codes:
            perm_id = perm_map[code]
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :perm_id)"
                ),
                {"role_id": role_id, "perm_id": perm_id},
            )


def downgrade() -> None:
    op.drop_index("idx_role_permissions_role_id", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_index("idx_permissions_resource_action", table_name="permissions")
    op.drop_table("permissions")

    op.drop_constraint("uq_role_org_priority", "roles", type_="unique")
    op.create_unique_constraint("uq_role_org_level", "roles", ["organization_id", "level"])
    op.alter_column("roles", "priority", new_column_name="level")
