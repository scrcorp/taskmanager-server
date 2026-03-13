"""add_sv_schedule_update_permission

Revision ID: 4a3290ce1174
Revises: cf467173f5f6
Create Date: 2026-03-13 13:52:35.390650

SV 역할에 schedules:update, schedules:delete 권한 추가
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4a3290ce1174'
down_revision: Union[str, None] = 'cf467173f5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SV_NEW_PERMS = ["schedules:update", "schedules:delete"]


def upgrade() -> None:
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
