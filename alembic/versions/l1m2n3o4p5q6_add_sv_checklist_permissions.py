"""add_sv_checklist_permissions

Revision ID: l1m2n3o4p5q6
Revises: k1l2m3n4o5p6
Create Date: 2026-02-27 12:00:00.000000

SV 역할에 checklists:create, checklists:update 권한 추가
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "l1m2n3o4p5q6"
down_revision: Union[str, None] = "k1l2m3n4o5p6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SV_NEW_PERMS = ["checklists:create", "checklists:update"]


def upgrade() -> None:
    conn = op.get_bind()

    # SV 역할 조회 (priority=30)
    sv_rows = conn.execute(
        sa.text("SELECT id FROM roles WHERE priority = 30")
    ).fetchall()

    # permission id 조회
    perm_rows = conn.execute(
        sa.text("SELECT id, code FROM permissions WHERE code = ANY(:codes)"),
        {"codes": SV_NEW_PERMS},
    ).fetchall()
    perm_map = {row[1]: row[0] for row in perm_rows}

    for sv_id, in sv_rows:
        for code in SV_NEW_PERMS:
            perm_id = perm_map.get(code)
            if not perm_id:
                continue
            # 중복 방지
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :perm_id) "
                    "ON CONFLICT ON CONSTRAINT uq_role_permission DO NOTHING"
                ),
                {"role_id": sv_id, "perm_id": perm_id},
            )


def downgrade() -> None:
    conn = op.get_bind()

    sv_rows = conn.execute(
        sa.text("SELECT id FROM roles WHERE priority = 30")
    ).fetchall()

    perm_rows = conn.execute(
        sa.text("SELECT id, code FROM permissions WHERE code = ANY(:codes)"),
        {"codes": SV_NEW_PERMS},
    ).fetchall()
    perm_map = {row[1]: row[0] for row in perm_rows}

    for sv_id, in sv_rows:
        for code in SV_NEW_PERMS:
            perm_id = perm_map.get(code)
            if not perm_id:
                continue
            conn.execute(
                sa.text(
                    "DELETE FROM role_permissions "
                    "WHERE role_id = :role_id AND permission_id = :perm_id"
                ),
                {"role_id": sv_id, "perm_id": perm_id},
            )
