"""create reports tables (multi-type)

Revision ID: 989f0fa82df1
Revises: 6f966454bea8
Create Date: 2026-05-11 15:21:19.446544

multi-type 통합 reports 인프라 추가. 기존 daily_reports* 테이블은 유지(별도 PR에서 백필 후 제거).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '989f0fa82df1'
down_revision: Union[str, None] = '6f966454bea8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'report_templates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('type', sa.String(length=32), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=True),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_report_templates_type'), 'report_templates', ['type'], unique=False)

    op.create_table(
        'reports',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('type', sa.String(length=32), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('template_id', sa.Uuid(), nullable=True),
        sa.Column('author_id', sa.Uuid(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('report_date', sa.Date(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['template_id'], ['report_templates.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reports_type'), 'reports', ['type'], unique=False)
    # daily 리포트 중복 방지 (store + date + period). 다른 type엔 영향 없음.
    op.create_index(
        'uq_reports_daily_store_date_period',
        'reports',
        ['store_id', 'report_date', sa.text("(payload->>'period')")],
        unique=True,
        postgresql_where=sa.text("type = 'daily' AND deleted_at IS NULL"),
    )
    # 자주 쓰는 조회: org + type + date
    op.create_index(
        'ix_reports_org_type_date',
        'reports',
        ['organization_id', 'type', sa.text('report_date DESC')],
    )

    op.create_table(
        'report_comments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('report_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('report_comments')
    op.drop_index('ix_reports_org_type_date', table_name='reports')
    op.drop_index('uq_reports_daily_store_date_period', table_name='reports')
    op.drop_index(op.f('ix_reports_type'), table_name='reports')
    op.drop_table('reports')
    op.drop_index(op.f('ix_report_templates_type'), table_name='report_templates')
    op.drop_table('report_templates')
