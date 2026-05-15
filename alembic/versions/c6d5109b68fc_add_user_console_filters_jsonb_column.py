"""add user.console_filters jsonb column

Revision ID: c6d5109b68fc
Revises: 5fa05f743f22
Create Date: 2026-05-15 13:42:13.006387

콘솔 UI 페이지별 필터/검색/정렬 상태를 사용자 단위로 영속화하기 위한 JSONB 컬럼.
1계정 1데이터 — 같은 사용자가 다른 디바이스로 로그인해도 동일한 필터를 본다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c6d5109b68fc'
down_revision: Union[str, None] = '5fa05f743f22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'console_filters',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='{}',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'console_filters')
