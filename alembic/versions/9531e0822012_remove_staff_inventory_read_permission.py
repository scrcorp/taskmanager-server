"""remove staff inventory read permission

Revision ID: 9531e0822012
Revises: a53d226ff8a7
Create Date: 2026-03-26 13:20:36.504337

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = '9531e0822012'
down_revision: Union[str, None] = 'a53d226ff8a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # Remove inventory:read from staff role
    conn.execute(text(
        "DELETE FROM role_permissions "
        "WHERE role_id = (SELECT id FROM roles WHERE name = 'staff') "
        "AND permission_id = (SELECT id FROM permissions WHERE code = 'inventory:read')"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    # Re-add inventory:read to staff role
    conn.execute(text(
        "INSERT INTO role_permissions (id, role_id, permission_id) "
        "SELECT gen_random_uuid(), r.id, p.id "
        "FROM roles r, permissions p "
        "WHERE r.name = 'staff' AND p.code = 'inventory:read' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM role_permissions rp "
        "  WHERE rp.role_id = r.id AND rp.permission_id = p.id"
        ")"
    ))
