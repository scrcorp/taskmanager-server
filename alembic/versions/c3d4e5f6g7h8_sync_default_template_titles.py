"""sync default template section titles from static JSON

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-03-04 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6g7h8'
down_revision: Union[str, None] = 'b2c3d4e5f6g7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEFAULT_TEMPLATE = {
    "name": "Supervisor Daily Report",
    "sections": [
        {"title": "Daily Sales", "description": "Sales figures, closing sales amount, performance against target", "sort_order": 1, "is_required": True},
        {"title": "Operations Business Ops / Service", "description": "POS issues, system problems, customer service matters, operational incidents", "sort_order": 2, "is_required": True},
        {"title": "Reservations", "description": "Reservation status, no-shows, special notes", "sort_order": 3, "is_required": False},
        {"title": "Staff", "description": "Attendance, schedule changes, call-outs, early departures, staffing issues", "sort_order": 4, "is_required": True},
        {"title": "Purchasing Procurement / Ordering", "description": "Order items, low-stock products, emergency purchases", "sort_order": 5, "is_required": True},
        {"title": "Cleaning Sanitation / Janitorial", "description": "Cleanliness status, sanitation inspection results, janitorial staff visit", "sort_order": 6, "is_required": True},
        {"title": "Facilities Maintenance / Equipment", "description": "Facility maintenance, equipment malfunctions, repair requests", "sort_order": 7, "is_required": False},
    ],
}


def upgrade() -> None:
    config = _DEFAULT_TEMPLATE

    conn = op.get_bind()

    # Update system-level default template (organization_id IS NULL, is_default=true)
    tmpl_row = conn.execute(sa.text(
        "SELECT id FROM daily_report_templates "
        "WHERE organization_id IS NULL AND is_default = true LIMIT 1"
    )).fetchone()

    if not tmpl_row:
        return

    template_id = tmpl_row[0]

    # Delete existing sections and re-create from JSON
    conn.execute(sa.text(
        "DELETE FROM daily_report_template_sections WHERE template_id = :tid"
    ), {"tid": template_id})

    for idx, s in enumerate(config["sections"], start=1):
        conn.execute(sa.text(
            "INSERT INTO daily_report_template_sections "
            "(id, template_id, title, description, sort_order, is_required, created_at) "
            "VALUES (gen_random_uuid(), :tid, :title, :desc, :sort, :req, now())"
        ), {
            "tid": template_id,
            "title": s["title"],
            "desc": s.get("description"),
            "sort": idx,
            "req": s.get("is_required", False),
        })

    # Update template name
    conn.execute(sa.text(
        "UPDATE daily_report_templates SET name = :name WHERE id = :tid"
    ), {"name": config["name"], "tid": template_id})


def downgrade() -> None:
    pass
