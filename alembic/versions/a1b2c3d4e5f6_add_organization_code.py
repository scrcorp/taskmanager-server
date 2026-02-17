"""add organization code column

Revision ID: a1b2c3d4e5f6
Revises: 2af5142285d3
Create Date: 2026-02-18 00:00:00.000000

"""
from typing import Sequence, Union

import random
import string

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "2af5142285d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _generate_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=6))


def upgrade() -> None:
    # 1) Add column as nullable first
    op.add_column("organizations", sa.Column("code", sa.String(6), nullable=True))

    # 2) Assign unique codes to existing rows
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id FROM organizations")).fetchall()
    used_codes: set[str] = set()
    for row in rows:
        code = _generate_code()
        while code in used_codes:
            code = _generate_code()
        used_codes.add(code)
        conn.execute(
            sa.text("UPDATE organizations SET code = :code WHERE id = :id"),
            {"code": code, "id": row[0]},
        )

    # 3) Make column NOT NULL and add unique constraint
    op.alter_column("organizations", "code", nullable=False)
    op.create_unique_constraint("uq_organizations_code", "organizations", ["code"])


def downgrade() -> None:
    op.drop_constraint("uq_organizations_code", "organizations", type_="unique")
    op.drop_column("organizations", "code")
