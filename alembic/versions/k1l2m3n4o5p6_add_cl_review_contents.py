"""add_cl_review_contents_and_remove_review_comment_photo

Revision ID: k1l2m3n4o5p6
Revises: 3119e9e8bbbc
Create Date: 2026-02-27 14:00:00.000000

cl_item_reviews에서 comment/photo_url 제거,
cl_review_contents 테이블 신규 생성.
기존 데이터가 있으면 마이그레이션.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'k1l2m3n4o5p6'
down_revision: Union[str, None] = '3119e9e8bbbc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. cl_review_contents 테이블 생성
    op.create_table('cl_review_contents',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('review_id', sa.Uuid(), nullable=False),
        sa.Column('author_id', sa.Uuid(), nullable=False),
        sa.Column('type', sa.String(length=10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['review_id'], ['cl_item_reviews.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cl_review_contents_review', 'cl_review_contents', ['review_id'])

    # 2. 기존 comment/photo_url 데이터를 cl_review_contents로 마이그레이션
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, reviewer_id, comment, photo_url FROM cl_item_reviews "
            "WHERE comment IS NOT NULL OR photo_url IS NOT NULL"
        )
    ).fetchall()

    for row in rows:
        review_id, reviewer_id, comment, photo_url = row
        if comment:
            conn.execute(
                sa.text(
                    "INSERT INTO cl_review_contents (id, review_id, author_id, type, content) "
                    "VALUES (gen_random_uuid(), :review_id, :author_id, 'text', :content)"
                ),
                {"review_id": review_id, "author_id": reviewer_id, "content": comment},
            )
        if photo_url:
            conn.execute(
                sa.text(
                    "INSERT INTO cl_review_contents (id, review_id, author_id, type, content) "
                    "VALUES (gen_random_uuid(), :review_id, :author_id, 'photo', :content)"
                ),
                {"review_id": review_id, "author_id": reviewer_id, "content": photo_url},
            )

    # 3. cl_item_reviews에서 comment, photo_url 컬럼 제거
    op.drop_column('cl_item_reviews', 'comment')
    op.drop_column('cl_item_reviews', 'photo_url')


def downgrade() -> None:
    # comment, photo_url 컬럼 복원
    op.add_column('cl_item_reviews', sa.Column('photo_url', sa.String(length=500), nullable=True))
    op.add_column('cl_item_reviews', sa.Column('comment', sa.Text(), nullable=True))

    # cl_review_contents 데이터를 다시 cl_item_reviews로 복원 (첫번째 text/photo만)
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE cl_item_reviews r SET comment = (
            SELECT content FROM cl_review_contents c
            WHERE c.review_id = r.id AND c.type = 'text'
            ORDER BY c.created_at LIMIT 1
        )
    """))
    conn.execute(sa.text("""
        UPDATE cl_item_reviews r SET photo_url = (
            SELECT content FROM cl_review_contents c
            WHERE c.review_id = r.id AND c.type = 'photo'
            ORDER BY c.created_at LIMIT 1
        )
    """))

    op.drop_index('ix_cl_review_contents_review', table_name='cl_review_contents')
    op.drop_table('cl_review_contents')
