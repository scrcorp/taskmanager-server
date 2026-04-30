"""applications history column

Revision ID: 202bd106f406
Revises: 3d526233861f
Create Date: 2026-04-30 16:59:28.994100

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '202bd106f406'
down_revision: Union[str, None] = '3d526233861f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'applications',
        sa.Column('history', postgresql.JSONB(astext_type=sa.Text()),
                  server_default='[]', nullable=False),
    )
    # NOTE: 이전 마이그레이션의 partial unique 'uq_store_form_one_draft'는 그대로 둔다.
    # autogenerate가 partial-where를 인식 못해 drop하려는 것을 막기 위해 명시 skip.


def downgrade() -> None:
    op.drop_column('applications', 'history')
