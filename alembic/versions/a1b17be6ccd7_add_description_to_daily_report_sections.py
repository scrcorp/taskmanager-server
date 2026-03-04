"""add description to daily_report_sections

Revision ID: a1b17be6ccd7
Revises: 697d72b113b7
Create Date: 2026-03-04 18:14:14.952552

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b17be6ccd7'
down_revision: Union[str, None] = '697d72b113b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('daily_report_sections', sa.Column('description', sa.Text(), nullable=True))

    # Backfill: copy description from template sections to existing report sections
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE daily_report_sections drs
        SET description = dts.description
        FROM daily_report_template_sections dts
        WHERE drs.template_section_id = dts.id
          AND drs.description IS NULL
          AND dts.description IS NOT NULL
    """))


def downgrade() -> None:
    op.drop_column('daily_report_sections', 'description')
