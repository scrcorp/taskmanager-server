"""grant daily_reports permissions to Staff role

Revision ID: 2f1210f9aceb
Revises: 8353eb507e17
Create Date: 2026-03-19 12:09:56.431569

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f1210f9aceb'
down_revision: Union[str, None] = '8353eb507e17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Staff gets create, read, update (same as SV, no delete)
STAFF_CODES = ["daily_reports:create", "daily_reports:read", "daily_reports:update"]
STAFF_PRIORITY = 40


def upgrade() -> None:
    conn = op.get_bind()

    perm_rows = conn.execute(sa.text(
        "SELECT id, code FROM permissions WHERE code = ANY(:codes)"
    ), {"codes": STAFF_CODES}).fetchall()
    perm_map = {row[1]: row[0] for row in perm_rows}

    staff_roles = conn.execute(sa.text(
        "SELECT id FROM roles WHERE priority = :p"
    ), {"p": STAFF_PRIORITY}).fetchall()

    for (role_id,) in staff_roles:
        for code in STAFF_CODES:
            pid = perm_map.get(code)
            if pid:
                conn.execute(sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (gen_random_uuid(), :rid, :pid, now()) "
                    "ON CONFLICT ON CONSTRAINT uq_role_permission DO NOTHING"
                ), {"rid": role_id, "pid": pid})


def downgrade() -> None:
    conn = op.get_bind()

    perm_rows = conn.execute(sa.text(
        "SELECT id FROM permissions WHERE code = ANY(:codes)"
    ), {"codes": STAFF_CODES}).fetchall()
    perm_ids = [row[0] for row in perm_rows]

    staff_roles = conn.execute(sa.text(
        "SELECT id FROM roles WHERE priority = :p"
    ), {"p": STAFF_PRIORITY}).fetchall()
    staff_role_ids = [row[0] for row in staff_roles]

    if perm_ids and staff_role_ids:
        conn.execute(sa.text(
            "DELETE FROM role_permissions WHERE role_id = ANY(:rids) AND permission_id = ANY(:pids)"
        ), {"rids": staff_role_ids, "pids": perm_ids})
