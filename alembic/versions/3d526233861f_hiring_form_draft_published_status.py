"""hiring form draft/published status

Revision ID: 3d526233861f
Revises: 645cb3a4f89c
Create Date: 2026-04-30 15:12:55.544941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d526233861f'
down_revision: Union[str, None] = '645cb3a4f89c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'store_hiring_forms',
        sa.Column('status', sa.String(length=20), server_default='published', nullable=False),
    )
    op.add_column(
        'store_hiring_forms',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )
    op.alter_column(
        'store_hiring_forms', 'version',
        existing_type=sa.INTEGER(),
        nullable=True,
    )
    # 매장당 draft는 0~1개로 제한
    op.create_index(
        'uq_store_form_one_draft',
        'store_hiring_forms',
        ['store_id'],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )


def downgrade() -> None:
    op.drop_index('uq_store_form_one_draft', table_name='store_hiring_forms')
    op.alter_column(
        'store_hiring_forms', 'version',
        existing_type=sa.INTEGER(),
        nullable=False,
    )
    op.drop_column('store_hiring_forms', 'updated_at')
    op.drop_column('store_hiring_forms', 'status')
