"""sync default template section titles from static JSON

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-03-04 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import json
from pathlib import Path


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6g7h8'
down_revision: Union[str, None] = 'b2c3d4e5f6g7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    config_path = Path(__file__).resolve().parent.parent.parent / "static" / "default_daily_report_template.json"
    with open(config_path) as f:
        config = json.load(f)

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
