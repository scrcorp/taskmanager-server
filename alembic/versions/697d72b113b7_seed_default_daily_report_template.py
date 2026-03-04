"""seed default daily report template per org

Revision ID: 697d72b113b7
Revises: 2414fb497698
Create Date: 2026-03-04 17:46:17.440272

Creates a default Daily Report template for each existing organization.
New organizations get theirs automatically via create_default_template_for_org().
"""
import json
from pathlib import Path
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '697d72b113b7'
down_revision: Union[str, None] = '2414fb497698'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _load_template_config():
    config_path = Path(__file__).resolve().parent.parent.parent / "static" / "default_daily_report_template.json"
    with open(config_path) as f:
        return json.load(f)


def upgrade() -> None:
    conn = op.get_bind()
    config = _load_template_config()

    # Remove system-level template if exists (from earlier approach)
    conn.execute(sa.text(
        "DELETE FROM daily_report_template_sections WHERE template_id IN "
        "(SELECT id FROM daily_report_templates WHERE organization_id IS NULL)"
    ))
    conn.execute(sa.text(
        "DELETE FROM daily_report_templates WHERE organization_id IS NULL"
    ))

    # Get all existing organizations
    orgs = conn.execute(sa.text("SELECT id FROM organizations")).fetchall()

    for (org_id,) in orgs:
        # Skip if org already has a template
        has = conn.execute(
            sa.text("SELECT 1 FROM daily_report_templates WHERE organization_id = :oid LIMIT 1"),
            {"oid": org_id},
        ).fetchone()
        if has:
            continue

        tid = str(uuid4())
        conn.execute(
            sa.text("""
                INSERT INTO daily_report_templates (id, organization_id, store_id, name, is_default, is_active, created_at, updated_at)
                VALUES (:id, :oid, NULL, :name, true, true, NOW(), NOW())
            """),
            {"id": tid, "oid": org_id, "name": config["name"]},
        )
        for s in config["sections"]:
            conn.execute(
                sa.text("""
                    INSERT INTO daily_report_template_sections (id, template_id, title, description, sort_order, is_required, created_at)
                    VALUES (:id, :tid, :title, :desc, :sort, :req, NOW())
                """),
                {"id": str(uuid4()), "tid": tid, "title": s["title"], "desc": s["description"], "sort": s["sort_order"], "req": s["is_required"]},
            )


def downgrade() -> None:
    conn = op.get_bind()
    # Remove all default templates (is_default=true, store_id IS NULL)
    conn.execute(sa.text(
        "DELETE FROM daily_report_template_sections WHERE template_id IN "
        "(SELECT id FROM daily_report_templates WHERE is_default = true AND store_id IS NULL)"
    ))
    conn.execute(sa.text(
        "DELETE FROM daily_report_templates WHERE is_default = true AND store_id IS NULL"
    ))
