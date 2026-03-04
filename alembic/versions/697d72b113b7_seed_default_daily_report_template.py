"""seed default daily report template

Revision ID: 697d72b113b7
Revises: 2414fb497698
Create Date: 2026-03-04 17:46:17.440272

"""
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '697d72b113b7'
down_revision: Union[str, None] = '2414fb497698'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed UUIDs for idempotency
TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    # Skip if already exists
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM daily_report_templates WHERE id = :id"),
        {"id": TEMPLATE_ID},
    ).fetchone()
    if exists:
        return

    conn.execute(
        sa.text("""
            INSERT INTO daily_report_templates (id, organization_id, store_id, name, is_default, is_active, created_at, updated_at)
            VALUES (:id, NULL, NULL, 'Daily Report (Default)', true, true, NOW(), NOW())
        """),
        {"id": TEMPLATE_ID},
    )

    sections = [
        ("Sales & Revenue", "Today's sales figures, transaction count, average ticket size, comparison to target", 1, True),
        ("Staff & Operations", "Staffing levels, attendance issues, notable performance, shift handoff notes", 2, True),
        ("Customer Feedback", "Customer complaints, compliments, special requests, service quality observations", 3, False),
        ("Issues & Actions", "Problems encountered, actions taken, unresolved issues requiring follow-up", 4, True),
        ("Notes", "Any other observations, reminders for next shift, upcoming events", 5, False),
    ]

    for title, desc, sort_order, is_required in sections:
        conn.execute(
            sa.text("""
                INSERT INTO daily_report_template_sections (id, template_id, title, description, sort_order, is_required, created_at)
                VALUES (:id, :tid, :title, :desc, :sort, :req, NOW())
            """),
            {"id": str(uuid4()), "tid": TEMPLATE_ID, "title": title, "desc": desc, "sort": sort_order, "req": is_required},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM daily_report_template_sections WHERE template_id = :id"),
        {"id": TEMPLATE_ID},
    )
    conn.execute(
        sa.text("DELETE FROM daily_report_templates WHERE id = :id"),
        {"id": TEMPLATE_ID},
    )
