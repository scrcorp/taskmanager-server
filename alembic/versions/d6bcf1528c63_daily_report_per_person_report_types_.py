"""daily report: per-person + report_types + applicable_types + deadline + review/ack

Revision ID: d6bcf1528c63
Revises: a82888275180
Create Date: 2026-06-29 12:15:14.805171

daily report configurable 리디자인 스키마 단계.
- per-person 유일성(결정-8): daily 중복 인덱스에 author_id 추가.
- report_types(결정-1/7/9): org-default + store override 타입 테이블 + 기존 org 시드.
- report_templates.applicable_types(결정-9): 적용 type code 배열.
- reports.deadline_at(P2), reviewed_by_id/reviewed_at(P3).
- report_acknowledgements(P3).

주의: autogenerate 가 announcements/notifications/notices/tasks 등 모델-DB drift 를
대량 오탐했으나, 그건 이 작업 범위가 아니므로 전부 제외했다. 또한 expression/partial
인덱스(ix_reports_org_type_date 등)는 alembic 이 reflect 못 해 drop 으로 오탐 → 보존.
legacy daily_reports* 테이블은 건드리지 않는다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd6bcf1528c63'
down_revision: Union[str, None] = 'a82888275180'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── report_types (org-default + store override) ──────────────────
    op.create_table(
        'report_types',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('default_deadline_local_time', sa.String(length=5), nullable=True),
        sa.Column('deadline_day_offset', sa.Integer(), server_default='0', nullable=False),
        sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_report_types_org_store', 'report_types', ['organization_id', 'store_id'], unique=False)
    # org-default 행 유일: (org, code) WHERE store_id IS NULL (살아있는 row 한정)
    op.create_index(
        'uq_report_types_org_code', 'report_types', ['organization_id', 'code'],
        unique=True, postgresql_where=sa.text('store_id IS NULL AND deleted_at IS NULL'),
    )
    # store override/add 행 유일: (org, store, code) WHERE store_id IS NOT NULL
    op.create_index(
        'uq_report_types_org_store_code', 'report_types', ['organization_id', 'store_id', 'code'],
        unique=True, postgresql_where=sa.text('store_id IS NOT NULL AND deleted_at IS NULL'),
    )

    # ── report_acknowledgements (P3) ─────────────────────────────────
    op.create_table(
        'report_acknowledgements',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('report_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('report_id', 'user_id', name='uq_report_ack_report_user'),
    )

    # ── report_templates.applicable_types (결정-9) ───────────────────
    op.add_column('report_templates', sa.Column('applicable_types', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    # ── reports: deadline_at (P2) + review meta (P3) ─────────────────
    op.add_column('reports', sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('reports', sa.Column('reviewed_by_id', sa.Uuid(), nullable=True))
    op.add_column('reports', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        'reports_reviewed_by_id_fkey', 'reports', 'users', ['reviewed_by_id'], ['id'], ondelete='SET NULL',
    )

    # ── per-person daily 유일성 (결정-8): author_id 추가 ──────────────
    # 기존 (store, date, period) 인덱스를 (store, date, period, author_id) 로 교체.
    op.drop_index(
        'uq_reports_daily_store_date_period', table_name='reports',
        postgresql_where="(((type)::text = 'daily'::text) AND (deleted_at IS NULL))",
    )
    op.create_index(
        'uq_reports_daily_store_date_period_author', 'reports',
        ['store_id', 'report_date', sa.text("(payload->>'period')"), 'author_id'],
        unique=True,
        postgresql_where=sa.text("type = 'daily' AND deleted_at IS NULL"),
    )

    # ── 기존 org 시드: lunch/dinner(active), morning(off by default) ──
    # 결정-7: morning 은 존재하나 기본 비활성. ON CONFLICT DO NOTHING 으로 멱등.
    op.execute("""
        INSERT INTO report_types (
            id, organization_id, store_id, code, label,
            sort_order, is_active, deadline_day_offset, is_deleted, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), o.id, NULL, v.code, v.label,
            v.sort_order, v.is_active, 0, false, now(), now()
        FROM organizations o
        CROSS JOIN (VALUES
            ('lunch',   'Lunch',   1, true),
            ('dinner',  'Dinner',  2, true),
            ('morning', 'Morning', 0, false)
        ) AS v(code, label, sort_order, is_active)
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # per-person 인덱스 → 원래 (store, date, period) 로 복원
    op.drop_index(
        'uq_reports_daily_store_date_period_author', table_name='reports',
        postgresql_where=sa.text("type = 'daily' AND deleted_at IS NULL"),
    )
    op.create_index(
        'uq_reports_daily_store_date_period', 'reports',
        ['store_id', 'report_date', sa.text("(payload->>'period')")],
        unique=True,
        postgresql_where=sa.text("type = 'daily' AND deleted_at IS NULL"),
    )

    op.drop_constraint('reports_reviewed_by_id_fkey', 'reports', type_='foreignkey')
    op.drop_column('reports', 'reviewed_at')
    op.drop_column('reports', 'reviewed_by_id')
    op.drop_column('reports', 'deadline_at')
    op.drop_column('report_templates', 'applicable_types')

    op.drop_table('report_acknowledgements')
    op.drop_index('uq_report_types_org_store_code', table_name='report_types', postgresql_where=sa.text('store_id IS NOT NULL AND deleted_at IS NULL'))
    op.drop_index('uq_report_types_org_code', table_name='report_types', postgresql_where=sa.text('store_id IS NULL AND deleted_at IS NULL'))
    op.drop_index('ix_report_types_org_store', table_name='report_types')
    op.drop_table('report_types')
