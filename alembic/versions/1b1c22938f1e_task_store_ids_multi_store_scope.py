"""task store_ids multi-store scope

Revision ID: 1b1c22938f1e
Revises: 0b49bd3c76e3
Create Date: 2026-05-18 23:16:40.686461

변경 사항:
- tasks 테이블에 store_ids JSONB 컬럼 추가 (default '[]')
- 기존 store_id 가 있는 task 는 store_ids = [store_id::text] 로 백필
- store_id 컬럼은 legacy mirror 로 그대로 둠 (NULL 가능)
- 빈 store_ids = org-wide (전사 task)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '1b1c22938f1e'
down_revision: Union[str, None] = '0b49bd3c76e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tasks',
        sa.Column(
            'store_ids',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='[]',
            nullable=False,
        ),
    )
    # 기존 store_id 가 있는 task 만 store_ids 채우기 (legacy → new format).
    op.execute(
        """
        UPDATE tasks
        SET store_ids = jsonb_build_array(store_id::text)
        WHERE store_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column('tasks', 'store_ids')
