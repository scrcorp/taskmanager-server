"""add_daily_reports

Revision ID: 2414fb497698
Revises: o1p2q3r4s5t6
Create Date: 2026-03-04 16:04:45.329474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2414fb497698'
down_revision: Union[str, None] = 'o1p2q3r4s5t6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Create tables ---
    op.create_table('daily_report_templates',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=True),
    sa.Column('store_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('daily_report_template_sections',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('template_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('is_required', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['template_id'], ['daily_report_templates.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('daily_reports',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('store_id', sa.Uuid(), nullable=False),
    sa.Column('template_id', sa.Uuid(), nullable=True),
    sa.Column('author_id', sa.Uuid(), nullable=False),
    sa.Column('report_date', sa.Date(), nullable=False),
    sa.Column('period', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['template_id'], ['daily_report_templates.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('store_id', 'report_date', 'period', name='uq_daily_report_store_date_period')
    )
    op.create_table('daily_report_comments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['daily_reports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('daily_report_sections',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('template_section_id', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['daily_reports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['template_section_id'], ['daily_report_template_sections.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )

    # --- Seed permissions ---
    conn = op.get_bind()
    permissions = [
        ("daily_reports:create", "daily_reports", "create", "Create daily reports"),
        ("daily_reports:read", "daily_reports", "read", "Read daily reports"),
        ("daily_reports:update", "daily_reports", "update", "Update daily reports"),
        ("daily_reports:delete", "daily_reports", "delete", "Delete daily reports"),
    ]
    for code, resource, action, desc in permissions:
        conn.execute(sa.text(
            "INSERT INTO permissions (id, code, resource, action, description, require_priority_check, created_at) "
            "VALUES (gen_random_uuid(), :code, :resource, :action, :desc, false, now()) "
            "ON CONFLICT (code) DO NOTHING"
        ), {"code": code, "resource": resource, "action": action, "desc": desc})

    perm_rows = conn.execute(sa.text(
        "SELECT id, code FROM permissions WHERE code = ANY(:codes)"
    ), {"codes": [p[0] for p in permissions]}).fetchall()
    perm_map = {row[1]: row[0] for row in perm_rows}

    # Owner (10) and GM (20) get all 4 permissions
    for priority in [10, 20]:
        role_rows = conn.execute(sa.text("SELECT id FROM roles WHERE priority = :p"), {"p": priority}).fetchall()
        for (role_id,) in role_rows:
            for code in perm_map:
                conn.execute(sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (gen_random_uuid(), :rid, :pid, now()) "
                    "ON CONFLICT ON CONSTRAINT uq_role_permission DO NOTHING"
                ), {"rid": role_id, "pid": perm_map[code]})

    # SV (30) gets create, read, update only
    sv_codes = ["daily_reports:create", "daily_reports:read", "daily_reports:update"]
    sv_roles = conn.execute(sa.text("SELECT id FROM roles WHERE priority = :p"), {"p": 30}).fetchall()
    for (role_id,) in sv_roles:
        for code in sv_codes:
            pid = perm_map.get(code)
            if pid:
                conn.execute(sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (gen_random_uuid(), :rid, :pid, now()) "
                    "ON CONFLICT ON CONSTRAINT uq_role_permission DO NOTHING"
                ), {"rid": role_id, "pid": pid})

    # --- Seed default template ---
    template_id = conn.execute(sa.text(
        "INSERT INTO daily_report_templates (id, organization_id, store_id, name, is_default, is_active, created_at, updated_at) "
        "VALUES (gen_random_uuid(), NULL, NULL, 'Supervisor Daily Report', true, true, now(), now()) "
        "RETURNING id"
    )).scalar()

    sections = [
        (1, "Daily Sales", "매출 현황을 기록하세요"),
        (2, "Operations", "운영 관련 사항을 기록하세요"),
        (3, "Reservations", "예약 현황을 기록하세요"),
        (4, "Staff", "직원 관련 사항을 기록하세요"),
        (5, "Purchasing", "구매/재고 관련 사항을 기록하세요"),
        (6, "Cleaning", "청결/위생 관련 사항을 기록하세요"),
        (7, "Facilities", "시설/설비 관련 사항을 기록하세요"),
    ]
    for sort_order, title, desc in sections:
        conn.execute(sa.text(
            "INSERT INTO daily_report_template_sections (id, template_id, title, description, sort_order, is_required, created_at) "
            "VALUES (gen_random_uuid(), :tid, :title, :desc, :sort, false, now())"
        ), {"tid": template_id, "title": title, "desc": desc, "sort": sort_order})


def downgrade() -> None:
    op.drop_table('daily_report_sections')
    op.drop_table('daily_report_comments')
    op.drop_table('daily_reports')
    op.drop_table('daily_report_template_sections')
    op.drop_table('daily_report_templates')

    conn = op.get_bind()
    # Remove seeded permissions and role-permission assignments
    perm_codes = [
        "daily_reports:create", "daily_reports:read",
        "daily_reports:update", "daily_reports:delete",
    ]
    conn.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE code = ANY(:codes))"
    ), {"codes": perm_codes})
    conn.execute(sa.text(
        "DELETE FROM permissions WHERE code = ANY(:codes)"
    ), {"codes": perm_codes})
