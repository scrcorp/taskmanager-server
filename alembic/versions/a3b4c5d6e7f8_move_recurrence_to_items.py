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
    # checklist_template_items에 recurrence 컬럼 추가
    # Note: checklist_templates에는 recurrence 컬럼이 존재하지 않으므로 데이터 마이그레이션 불필요
    op.add_column(
        'checklist_template_items',
        sa.Column('recurrence_type', sa.String(10), server_default='daily', nullable=False),
    )
    op.add_column(
        'checklist_template_items',
        sa.Column('recurrence_days', JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('checklist_template_items', 'recurrence_type')
    op.drop_column('checklist_template_items', 'recurrence_days')
