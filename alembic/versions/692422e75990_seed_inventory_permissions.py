"""seed inventory permissions

Revision ID: 692422e75990
Revises: 8a5afa07e3e1
Create Date: 2026-03-25 09:31:26.288331

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = '692422e75990'
down_revision: Union[str, None] = '8a5afa07e3e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS = [
    ("inventory:read", "inventory", "read", "View inventory products and stock levels", False),
    ("inventory:create", "inventory", "create", "Create products, stock in/out, audits", False),
    ("inventory:update", "inventory", "update", "Update products, stock settings", False),
    ("inventory:delete", "inventory", "delete", "Deactivate products, remove store inventory", False),
]

# Role assignments: Owner/GM = all 4, SV = read+create, Staff = read
ROLE_ASSIGNMENTS = {
    "owner": ["inventory:read", "inventory:create", "inventory:update", "inventory:delete"],
    "general_manager": ["inventory:read", "inventory:create", "inventory:update", "inventory:delete"],
    "supervisor": ["inventory:read", "inventory:create"],
    "staff": ["inventory:read"],
}


def upgrade() -> None:
    conn = op.get_bind()

    # Insert permissions
    for code, resource, action, description, require_priority in PERMISSIONS:
        conn.execute(text(
            "INSERT INTO permissions (id, code, resource, action, description, require_priority_check) "
            "VALUES (gen_random_uuid(), :code, :resource, :action, :description, :require_priority) "
            "ON CONFLICT DO NOTHING"
        ), {"code": code, "resource": resource, "action": action, "description": description, "require_priority": require_priority})

    # Assign to roles
    for role_name, perm_codes in ROLE_ASSIGNMENTS.items():
        for perm_code in perm_codes:
            conn.execute(text(
                "INSERT INTO role_permissions (id, role_id, permission_id) "
                "SELECT gen_random_uuid(), r.id, p.id "
                "FROM roles r, permissions p "
                "WHERE r.name = :role_name AND p.code = :perm_code "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM role_permissions rp "
                "  WHERE rp.role_id = r.id AND rp.permission_id = p.id"
                ")"
            ), {"role_name": role_name, "perm_code": perm_code})


def downgrade() -> None:
    conn = op.get_bind()
    codes = [p[0] for p in PERMISSIONS]
    for code in codes:
        conn.execute(text(
            "DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code = :code)"
        ), {"code": code})
        conn.execute(text(
            "DELETE FROM permissions WHERE code = :code"
        ), {"code": code})
