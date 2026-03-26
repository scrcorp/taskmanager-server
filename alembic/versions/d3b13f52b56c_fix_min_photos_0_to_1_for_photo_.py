"""fix min_photos 0 to 1 for photo required items

Revision ID: d3b13f52b56c
Revises: 77ac713c4247
Create Date: 2026-03-26 11:15:20.918722

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3b13f52b56c'
down_revision: Union[str, None] = '77ac713c4247'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # photo required items with min_photos=0 should be at least 1
    op.execute(sa.text("""
        UPDATE cl_instance_items
        SET min_photos = 1
        WHERE verification_type LIKE '%photo%'
          AND min_photos = 0
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        UPDATE cl_instance_items
        SET min_photos = 0
        WHERE verification_type LIKE '%photo%'
          AND min_photos = 1
    """))
