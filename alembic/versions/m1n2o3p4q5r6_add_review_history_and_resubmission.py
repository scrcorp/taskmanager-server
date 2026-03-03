"""add_review_history_and_resubmission

Revision ID: m1n2o3p4q5r6
Revises: l1m2n3o4p5q6
Create Date: 2026-03-03

New tables: cl_review_history, cl_completion_history
Modified: cl_completions (add resubmission_count, updated_at)
Modified: cl_item_reviews.result column width (10 -> 20)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'm1n2o3p4q5r6'
down_revision: Union[str, None] = 'l1m2n3o4p5q6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. cl_review_history 테이블 생성
    op.create_table('cl_review_history',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('review_id', sa.Uuid(), nullable=False),
        sa.Column('changed_by', sa.Uuid(), nullable=False),
        sa.Column('old_result', sa.String(length=20), nullable=True),
        sa.Column('new_result', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['review_id'], ['cl_item_reviews.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # 2. cl_completion_history 테이블 생성
    op.create_table('cl_completion_history',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('completion_id', sa.Uuid(), nullable=False),
        sa.Column('photo_url', sa.String(length=500), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('location', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['completion_id'], ['cl_completions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # 3. cl_completions에 resubmission_count, updated_at 추가
    op.add_column('cl_completions', sa.Column('resubmission_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('cl_completions', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()))

    # 4. cl_item_reviews.result 컬럼 폭 확대 (10 -> 20)
    op.alter_column('cl_item_reviews', 'result',
                     type_=sa.String(length=20),
                     existing_type=sa.String(length=10),
                     existing_nullable=False)


def downgrade() -> None:
    op.alter_column('cl_item_reviews', 'result',
                     type_=sa.String(length=10),
                     existing_type=sa.String(length=20),
                     existing_nullable=False)
    op.drop_column('cl_completions', 'updated_at')
    op.drop_column('cl_completions', 'resubmission_count')
    op.drop_table('cl_completion_history')
    op.drop_table('cl_review_history')
