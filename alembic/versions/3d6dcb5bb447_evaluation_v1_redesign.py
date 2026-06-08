"""evaluation v1 redesign

JSONB/snapshot 기반 평가 v1 재설계. 레거시 정규화 스택(eval_template_items +
eval_responses)을 버리고 2-테이블(eval_templates, evaluations)로 재구축한다.

안전 게이트(마이그레이션 전 수동 확인): SELECT count(*) FROM evaluations == 0.
v1 시점 4개 레거시 평가 테이블은 모두 비어 있으므로(0 rows) drop/recreate 가 안전.

upgrade:
  1. 레거시 4개 평가 테이블 drop (FK 의존 순서: eval_responses → eval_template_items
     → evaluations → eval_templates).
  2. 새 eval_templates, evaluations 생성 (FK 순서: eval_templates 먼저).
  3. users.employee_no 추가 + partial unique index (WHERE employee_no IS NOT NULL).

downgrade: 위를 역순으로. 레거시 4개 테이블은 best-effort 재생성(미사용이었음).

NOTE: autogenerate 가 감지한 평가 외 drift(announcements/notifications/
announcement_reads, users.notification_preferences, 일부 partial index)는 이 마이그레이션
범위가 아니므로 의도적으로 제외했다.

Revision ID: 3d6dcb5bb447
Revises: 7e06ae4ec636
Create Date: 2026-06-08 16:11:51.305900

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3d6dcb5bb447'
down_revision: Union[str, None] = '7e06ae4ec636'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. 레거시 4개 평가 테이블 drop (FK 의존 순서) ──────────────────
    op.drop_table('eval_responses')
    op.drop_table('eval_template_items')
    op.drop_table('evaluations')
    op.drop_table('eval_templates')

    # ── 2-a. 새 eval_templates 생성 (evaluations 보다 먼저) ──────────────
    op.create_table(
        'eval_templates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='published', nullable=False),
        sa.Column('is_current', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['created_by_user_id'], ['users.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'organization_id', 'version', name='uq_eval_template_org_version'
        ),
    )
    op.create_index(
        'ix_eval_templates_organization_id', 'eval_templates', ['organization_id'], unique=False
    )

    # ── 2-b. 새 evaluations 생성 ─────────────────────────────────────────
    op.create_table(
        'evaluations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('evaluator_id', sa.Uuid(), nullable=True),
        sa.Column('evaluatee_id', sa.Uuid(), nullable=True),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('position_id', sa.Uuid(), nullable=True),
        sa.Column('job_title', sa.String(length=255), nullable=True),
        sa.Column('template_id', sa.Uuid(), nullable=True),
        sa.Column('template_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('responses', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('improvement', sa.Text(), nullable=True),
        sa.Column('good_examples', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['evaluator_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['evaluatee_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['template_id'], ['eval_templates.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_evaluations_organization_id', 'evaluations', ['organization_id'], unique=False
    )
    op.create_index(
        'ix_evaluations_evaluatee_id', 'evaluations', ['evaluatee_id'], unique=False
    )
    op.create_index(
        'ix_evaluations_org_deleted', 'evaluations', ['organization_id', 'deleted_at'], unique=False
    )

    # ── 3. users.employee_no + partial unique index ─────────────────────
    op.add_column('users', sa.Column('employee_no', sa.String(length=50), nullable=True))
    op.create_index(
        'uq_user_org_employee_no',
        'users',
        ['organization_id', 'employee_no'],
        unique=True,
        postgresql_where=sa.text('employee_no IS NOT NULL'),
    )


def downgrade() -> None:
    # ── 3. users.employee_no 제거 ───────────────────────────────────────
    op.drop_index(
        'uq_user_org_employee_no',
        table_name='users',
        postgresql_where=sa.text('employee_no IS NOT NULL'),
    )
    op.drop_column('users', 'employee_no')

    # ── 2. 새 2개 테이블 drop (FK 역순: evaluations 먼저) ─────────────────
    op.drop_index('ix_evaluations_org_deleted', table_name='evaluations')
    op.drop_index('ix_evaluations_evaluatee_id', table_name='evaluations')
    op.drop_index('ix_evaluations_organization_id', table_name='evaluations')
    op.drop_table('evaluations')
    op.drop_index('ix_eval_templates_organization_id', table_name='eval_templates')
    op.drop_table('eval_templates')

    # ── 1. 레거시 4개 테이블 best-effort 재생성 (FK 순서: 부모 먼저) ──────
    op.create_table(
        'eval_templates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('target_role', sa.String(length=50), nullable=True),
        sa.Column('eval_type', sa.String(length=20), server_default=sa.text("'adhoc'::character varying"), nullable=False),
        sa.Column('cycle_weeks', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'evaluations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('evaluator_id', sa.Uuid(), nullable=True),
        sa.Column('evaluatee_id', sa.Uuid(), nullable=True),
        sa.Column('template_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['evaluator_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['evaluatee_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['template_id'], ['eval_templates.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'eval_template_items',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('template_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('type', sa.String(length=20), server_default=sa.text("'score'::character varying"), nullable=False),
        sa.Column('max_score', sa.Integer(), server_default=sa.text('5'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['template_id'], ['eval_templates.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'eval_responses',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('evaluation_id', sa.Uuid(), nullable=False),
        sa.Column('template_item_id', sa.Uuid(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['evaluation_id'], ['evaluations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_item_id'], ['eval_template_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('evaluation_id', 'template_item_id', name='uq_eval_response_eval_item'),
    )
