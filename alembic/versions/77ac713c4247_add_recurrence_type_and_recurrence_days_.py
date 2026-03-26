"""add recurrence_type and recurrence_days to cl_instance_items

Revision ID: 77ac713c4247
Revises: a53d226ff8a7
Create Date: 2026-03-26 10:16:41.460447

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '77ac713c4247'
down_revision: Union[str, None] = 'a53d226ff8a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add columns (nullable, no default)
    op.add_column('cl_instance_items', sa.Column('recurrence_type', sa.String(length=10), nullable=True))
    op.add_column('cl_instance_items', sa.Column('recurrence_days', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    # 2. Backfill from template items — match by instance.template_id + item title
    op.execute(sa.text("""
        UPDATE cl_instance_items
        SET recurrence_type = ti.recurrence_type,
            recurrence_days = ti.recurrence_days
        FROM cl_instances i, checklist_template_items ti
        WHERE cl_instance_items.instance_id = i.id
          AND ti.template_id = i.template_id
          AND ti.title = cl_instance_items.title
    """))


def downgrade() -> None:
    op.drop_column('cl_instance_items', 'recurrence_days')
    op.drop_column('cl_instance_items', 'recurrence_type')
