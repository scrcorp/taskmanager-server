"""add changelog_posts table

Revision ID: db52ad29a2f5
Revises: d6bcf1528c63
Create Date: 2026-06-29 16:51:04.145075

NOTE: autogenerate가 dev DB 스냅샷↔현재 모델의 기존 드리프트(announcements/
notifications 등 이미 제거된 테이블)를 함께 잡았으나, 본 마이그레이션은 changelog
추가만 담당하므로 무관한 drop/recreate 는 전부 제거했다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'db52ad29a2f5'
down_revision: Union[str, None] = 'd6bcf1528c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'changelog_posts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('slug', sa.String(length=120), nullable=False),
        sa.Column('category', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('cover_image_key', sa.String(length=500), nullable=True),
        sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_published', sa.Boolean(), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index(
        'ix_changelog_category_published_at',
        'changelog_posts',
        ['category', 'published_at'],
        unique=False,
        postgresql_where=sa.text('is_published IS TRUE'),
    )
    op.create_index(
        op.f('ix_changelog_posts_category'), 'changelog_posts', ['category'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_changelog_posts_category'), table_name='changelog_posts')
    op.drop_index(
        'ix_changelog_category_published_at',
        table_name='changelog_posts',
        postgresql_where=sa.text('is_published IS TRUE'),
    )
    op.drop_table('changelog_posts')
