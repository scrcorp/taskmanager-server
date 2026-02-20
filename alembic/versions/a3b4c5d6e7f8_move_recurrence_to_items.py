"""move_recurrence_from_templates_to_items

Revision ID: a3b4c5d6e7f8
Revises: 2f9cb0f822b4
Create Date: 2026-02-20 18:00:00.000000

recurrence_type / recurrence_days 를 checklist_templates → checklist_template_items 로 이동.
기존 template 레벨 recurrence 값을 해당 template의 모든 item에 복사한 뒤 template 컬럼 제거.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = '2f9cb0f822b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. checklist_template_items에 recurrence 컬럼 추가
    op.add_column(
        'checklist_template_items',
        sa.Column('recurrence_type', sa.String(10), server_default='daily', nullable=False),
    )
    op.add_column(
        'checklist_template_items',
        sa.Column('recurrence_days', JSONB, nullable=True),
    )

    # 2. 기존 template 레벨 recurrence → item 레벨로 데이터 마이그레이션
    op.execute("""
        UPDATE checklist_template_items AS i
        SET recurrence_type = t.recurrence_type,
            recurrence_days = t.recurrence_days
        FROM checklist_templates AS t
        WHERE i.template_id = t.id
          AND t.recurrence_type IS NOT NULL
    """)

    # 3. checklist_templates에서 recurrence 컬럼 제거
    op.drop_column('checklist_templates', 'recurrence_type')
    op.drop_column('checklist_templates', 'recurrence_days')


def downgrade() -> None:
    # 1. checklist_templates에 recurrence 컬럼 복원
    op.add_column(
        'checklist_templates',
        sa.Column('recurrence_type', sa.String(10), server_default='daily', nullable=False),
    )
    op.add_column(
        'checklist_templates',
        sa.Column('recurrence_days', JSONB, nullable=True),
    )

    # 2. item 레벨 recurrence → template 레벨로 역마이그레이션 (첫 번째 item 기준)
    op.execute("""
        UPDATE checklist_templates AS t
        SET recurrence_type = sub.recurrence_type,
            recurrence_days = sub.recurrence_days
        FROM (
            SELECT DISTINCT ON (template_id)
                   template_id, recurrence_type, recurrence_days
            FROM checklist_template_items
            ORDER BY template_id, sort_order
        ) AS sub
        WHERE t.id = sub.template_id
    """)

    # 3. checklist_template_items에서 recurrence 컬럼 제거
    op.drop_column('checklist_template_items', 'recurrence_type')
    op.drop_column('checklist_template_items', 'recurrence_days')
