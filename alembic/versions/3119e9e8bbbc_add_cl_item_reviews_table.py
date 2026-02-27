"""add_cl_item_reviews_table

Revision ID: 3119e9e8bbbc
Revises: j1k2l3m4n5o6
Create Date: 2026-02-27 11:23:44.268376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3119e9e8bbbc'
down_revision: Union[str, None] = 'j1k2l3m4n5o6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('cl_item_reviews',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('instance_id', sa.Uuid(), nullable=False),
        sa.Column('item_index', sa.Integer(), nullable=False),
        sa.Column('reviewer_id', sa.Uuid(), nullable=False),
        sa.Column('result', sa.String(length=10), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('photo_url', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['instance_id'], ['cl_instances.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('instance_id', 'item_index', name='uq_cl_item_review_instance_item'),
    )
    op.create_index('ix_cl_item_reviews_instance', 'cl_item_reviews', ['instance_id'])


def downgrade() -> None:
    op.drop_index('ix_cl_item_reviews_instance', table_name='cl_item_reviews')
    op.drop_table('cl_item_reviews')
