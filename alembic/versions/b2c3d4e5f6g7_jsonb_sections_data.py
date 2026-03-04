"""migrate daily_report_sections to JSONB sections_data

Revision ID: b2c3d4e5f6g7
Revises: a1b17be6ccd7
Create Date: 2026-03-04 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g7'
down_revision: Union[str, None] = 'a1b17be6ccd7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add sections_data JSONB column
    op.add_column('daily_reports', sa.Column('sections_data', JSONB, nullable=True, server_default='[]'))

    # 2. Migrate existing data: aggregate daily_report_sections into JSONB per report
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE daily_reports dr
        SET sections_data = sub.sections_json
        FROM (
            SELECT
                report_id,
                jsonb_agg(
                    jsonb_build_object(
                        'title', title,
                        'description', description,
                        'content', content,
                        'sort_order', sort_order,
                        'is_required', false
                    )
                    ORDER BY sort_order
                ) AS sections_json
            FROM daily_report_sections
            GROUP BY report_id
        ) sub
        WHERE dr.id = sub.report_id
    """))

    # 3. Set empty array for reports with no sections
    conn.execute(sa.text("""
        UPDATE daily_reports SET sections_data = '[]'::jsonb WHERE sections_data IS NULL
    """))

    # 4. Drop the old table
    op.drop_table('daily_report_sections')


def downgrade() -> None:
    # Recreate daily_report_sections table
    op.create_table('daily_report_sections',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('report_id', sa.Uuid(), nullable=False),
        sa.Column('template_section_id', sa.Uuid(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['report_id'], ['daily_reports.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_section_id'], ['daily_report_template_sections.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Drop JSONB column
    op.drop_column('daily_reports', 'sections_data')
