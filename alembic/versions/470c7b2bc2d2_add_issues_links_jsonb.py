"""add issues.links jsonb

Revision ID: 470c7b2bc2d2
Revises: fb3480f91eb1
Create Date: 2026-05-11 17:25:00.000000

issues 테이블에 links jsonb 컬럼 추가.
{"schedule_ids": [...], "checklist_instance_ids": [...], "position_ids": [...], "work_role_ids": [...]}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '470c7b2bc2d2'
down_revision: Union[str, None] = 'fb3480f91eb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'issues',
        sa.Column(
            'links',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='{}',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('issues', 'links')
