"""warning v1.1: warning_categories table + warnings other_text/deadline/follow_up

Revision ID: e482973c572c
Revises: 08df66aeb81c
Create Date: 2026-06-10 17:10:54.282218

내용:
    - warning_categories 테이블 신설 (org별 사유 카테고리 — code/label/sort/hidden/system, soft delete)
    - warnings: other_text, deadline, follow_up_date, follow_up_time 컬럼 추가
    - 기존 org 전부에 기본 카테고리 12종 backfill (app.core.warning.DEFAULT_WARNING_CATEGORIES 스냅샷)

주의: autogenerate 가 잡은 무관한 drop(announcements/notifications/인덱스 등 dev DB 드리프트)은
모두 제거했다. 이 마이그레이션은 위 의도된 변경만 수행한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e482973c572c'
down_revision: Union[str, None] = '08df66aeb81c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 마이그레이션 시점 스냅샷 — (code, label, is_hidden, is_system). 양식 컬럼 순.
DEFAULT_CATEGORIES: list[tuple[str, str, bool, bool]] = [
    ("tardiness", "Tardiness", False, False),
    ("damaged_equipment", "Damaged equipment", False, False),
    ("refusal_overtime", "Refusal to work overtime", True, False),
    ("absenteeism", "Absenteeism", False, False),
    ("policy_violation", "Policy violation", False, False),
    ("insubordination", "Insubordination", False, False),
    ("rudeness", "Rudeness", False, False),
    ("fighting", "Fighting", False, False),
    ("language", "Language", False, False),
    ("failure_procedure", "Failure to follow procedure", False, False),
    ("failure_performance", "Failure to meet performance standards", False, False),
    ("other", "Other", False, True),
]
_SYSTEM_SORT = 9000


def upgrade() -> None:
    # 1) warning_categories 테이블
    op.create_table(
        'warning_categories',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('is_hidden', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_system', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'code', name='uq_warning_category_org_code'),
    )
    op.create_index('ix_warning_categories_org_deleted', 'warning_categories', ['organization_id', 'deleted_at'], unique=False)
    op.create_index(op.f('ix_warning_categories_organization_id'), 'warning_categories', ['organization_id'], unique=False)

    # 2) warnings 새 컬럼
    op.add_column('warnings', sa.Column('other_text', sa.Text(), nullable=True))
    op.add_column('warnings', sa.Column('deadline', sa.Date(), nullable=True))
    op.add_column('warnings', sa.Column('follow_up_date', sa.Date(), nullable=True))
    op.add_column('warnings', sa.Column('follow_up_time', sa.Time(), nullable=True))

    # 3) 기존 org 전부에 기본 카테고리 backfill (idempotent — ON CONFLICT DO NOTHING)
    conn = op.get_bind()
    org_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM organizations")).fetchall()]
    insert_sql = sa.text(
        """
        INSERT INTO warning_categories
            (id, organization_id, code, label, sort_order, is_hidden, is_system, created_at, updated_at)
        VALUES
            (gen_random_uuid(), :org_id, :code, :label, :sort_order, :is_hidden, :is_system, now(), now())
        ON CONFLICT (organization_id, code) DO NOTHING
        """
    )
    for org_id in org_ids:
        for i, (code, label, is_hidden, is_system) in enumerate(DEFAULT_CATEGORIES):
            sort_order = _SYSTEM_SORT if is_system else (i + 1) * 10
            conn.execute(
                insert_sql,
                {
                    "org_id": org_id,
                    "code": code,
                    "label": label,
                    "sort_order": sort_order,
                    "is_hidden": is_hidden,
                    "is_system": is_system,
                },
            )


def downgrade() -> None:
    op.drop_column('warnings', 'follow_up_time')
    op.drop_column('warnings', 'follow_up_date')
    op.drop_column('warnings', 'deadline')
    op.drop_column('warnings', 'other_text')
    op.drop_index(op.f('ix_warning_categories_organization_id'), table_name='warning_categories')
    op.drop_index('ix_warning_categories_org_deleted', table_name='warning_categories')
    op.drop_table('warning_categories')
