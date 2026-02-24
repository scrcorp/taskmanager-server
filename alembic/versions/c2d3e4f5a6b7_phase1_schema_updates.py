"""phase1_schema_updates

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-02-24 22:00:00.000000

Phase 1 스키마 변경:
1. roles.level *10 (1-4 → 10-40)
2. stores 컬럼 추가 (operating_hours, max_work_hours_weekly, state_code)
3. 신규 테이블: shift_presets, cl_comments, labor_law_settings,
   announcement_reads, eval_templates, eval_template_items,
   evaluations, eval_responses
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. roles.level *10 마이그레이션 ──
    op.execute("UPDATE roles SET level = level * 10 WHERE level < 10")

    # ── 2. stores 테이블 컬럼 추가 ──
    op.add_column('stores', sa.Column('operating_hours', JSONB, nullable=True))
    op.add_column('stores', sa.Column('max_work_hours_weekly', sa.Integer(), nullable=True))
    op.add_column('stores', sa.Column('state_code', sa.String(10), nullable=True))

    # ── 3. shift_presets — 시프트 프리셋 ──
    op.create_table(
        'shift_presets',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('shift_id', UUID(as_uuid=True), sa.ForeignKey('shifts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_shift_presets_store_shift', 'shift_presets', ['store_id', 'shift_id'])

    # ── 4. cl_comments — 체크리스트 코멘트 ──
    op.create_table(
        'cl_comments',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('instance_id', UUID(as_uuid=True), sa.ForeignKey('cl_instances.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_cl_comments_instance', 'cl_comments', ['instance_id'])

    # ── 5. labor_law_settings — 노동법 설정 ──
    op.create_table(
        'labor_law_settings',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('federal_max_weekly', sa.Integer(), server_default=sa.text('40'), nullable=False),
        sa.Column('state_max_weekly', sa.Integer(), nullable=True),
        sa.Column('store_max_weekly', sa.Integer(), nullable=True),
        sa.Column('overtime_threshold_daily', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_unique_constraint('uq_labor_law_org_store', 'labor_law_settings', ['organization_id', 'store_id'])

    # ── 6. announcement_reads — 공지사항 읽음 추적 ──
    op.create_table(
        'announcement_reads',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('announcement_id', UUID(as_uuid=True), sa.ForeignKey('announcements.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_unique_constraint('uq_announcement_read_user', 'announcement_reads', ['announcement_id', 'user_id'])

    # ── 7. eval_templates — 평가 템플릿 ──
    op.create_table(
        'eval_templates',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('target_role', sa.String(50), nullable=True),
        sa.Column('eval_type', sa.String(20), server_default=sa.text("'adhoc'"), nullable=False),
        sa.Column('cycle_weeks', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 8. eval_template_items — 평가 항목 ──
    op.create_table(
        'eval_template_items',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('template_id', UUID(as_uuid=True), sa.ForeignKey('eval_templates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('type', sa.String(20), server_default=sa.text("'score'"), nullable=False),
        sa.Column('max_score', sa.Integer(), server_default=sa.text('5'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── 9. evaluations — 평가 본체 ──
    op.create_table(
        'evaluations',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='SET NULL'), nullable=True),
        sa.Column('evaluator_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('evaluatee_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('template_id', UUID(as_uuid=True), sa.ForeignKey('eval_templates.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_evaluations_org_evaluatee', 'evaluations', ['organization_id', 'evaluatee_id'])

    # ── 10. eval_responses — 평가 응답 ──
    op.create_table(
        'eval_responses',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('evaluation_id', UUID(as_uuid=True), sa.ForeignKey('evaluations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('template_item_id', UUID(as_uuid=True), sa.ForeignKey('eval_template_items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_unique_constraint('uq_eval_response_eval_item', 'eval_responses', ['evaluation_id', 'template_item_id'])


def downgrade() -> None:
    op.drop_table('eval_responses')
    op.drop_table('evaluations')
    op.drop_table('eval_template_items')
    op.drop_table('eval_templates')
    op.drop_table('announcement_reads')
    op.drop_table('labor_law_settings')
    op.drop_table('cl_comments')
    op.drop_table('shift_presets')
    op.drop_column('stores', 'state_code')
    op.drop_column('stores', 'max_work_hours_weekly')
    op.drop_column('stores', 'operating_hours')
    op.execute("UPDATE roles SET level = level / 10 WHERE level >= 10")
