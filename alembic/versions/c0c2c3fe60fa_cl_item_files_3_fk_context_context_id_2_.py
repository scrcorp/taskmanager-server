"""cl_item_files: 3 FK → context + context_id (2 columns)

Revision ID: c0c2c3fe60fa
Revises: 93bcc7ba20e8
Create Date: 2026-03-17 16:26:46.369240

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0c2c3fe60fa'
down_revision: Union[str, None] = '93bcc7ba20e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add new columns (context nullable temporarily for data migration)
    op.add_column('cl_item_files', sa.Column('context', sa.String(length=20), nullable=True))
    op.add_column('cl_item_files', sa.Column('context_id', sa.Uuid(), nullable=True))

    # 2. Migrate existing FK data → context + context_id
    op.execute("""
        UPDATE cl_item_files
        SET context = 'submission', context_id = submission_id
        WHERE submission_id IS NOT NULL
    """)
    op.execute("""
        UPDATE cl_item_files
        SET context = 'review', context_id = review_log_id
        WHERE review_log_id IS NOT NULL
    """)
    op.execute("""
        UPDATE cl_item_files
        SET context = 'chat', context_id = message_id
        WHERE message_id IS NOT NULL
    """)
    # Files with no FK → default to 'submission' (current submission files)
    op.execute("""
        UPDATE cl_item_files
        SET context = 'submission'
        WHERE context IS NULL
    """)

    # 3. Migrate cl_item_messages with photo/video URLs → cl_item_files
    # (messages that had type='photo' or 'video' before type column was removed)
    # The previous migration (93bcc7ba20e8) removed the type column but didn't migrate data.
    # Messages with content starting with http or containing file extensions are likely file URLs.
    # We check if content looks like a URL (contains / and ends with image/video extension)
    op.execute("""
        INSERT INTO cl_item_files (id, item_id, context, context_id, file_url, file_type, sort_order, uploaded_by, created_at)
        SELECT
            gen_random_uuid(),
            m.item_id,
            'chat',
            m.id,
            m.content,
            CASE
                WHEN m.content ~* '\\.(mp4|mov|webm|avi|mkv)' THEN 'video'
                ELSE 'photo'
            END,
            0,
            m.author_id,
            m.created_at
        FROM cl_item_messages m
        WHERE m.content IS NOT NULL
          AND (m.content LIKE '%/%' AND m.content ~* '\\.(jpg|jpeg|png|gif|webp|mp4|mov|webm|avi|mkv)')
    """)

    # Clear content for messages that were actually file URLs (now in cl_item_files)
    op.execute("""
        UPDATE cl_item_messages
        SET content = NULL
        WHERE content IS NOT NULL
          AND (content LIKE '%/%' AND content ~* '\\.(jpg|jpeg|png|gif|webp|mp4|mov|webm|avi|mkv)')
    """)

    # 4. Make context NOT NULL
    op.alter_column('cl_item_files', 'context', nullable=False)

    # 5. Create index
    op.create_index('ix_cl_item_files_context', 'cl_item_files', ['context', 'context_id'], unique=False)

    # 6. Drop old FK columns
    op.drop_constraint('cl_item_files_submission_id_fkey', 'cl_item_files', type_='foreignkey')
    op.drop_constraint('cl_item_files_message_id_fkey', 'cl_item_files', type_='foreignkey')
    op.drop_constraint('cl_item_files_review_log_id_fkey', 'cl_item_files', type_='foreignkey')
    op.drop_column('cl_item_files', 'message_id')
    op.drop_column('cl_item_files', 'review_log_id')
    op.drop_column('cl_item_files', 'submission_id')


def downgrade() -> None:
    op.add_column('cl_item_files', sa.Column('submission_id', sa.UUID(), nullable=True))
    op.add_column('cl_item_files', sa.Column('review_log_id', sa.UUID(), nullable=True))
    op.add_column('cl_item_files', sa.Column('message_id', sa.UUID(), nullable=True))

    # Restore FK data from context
    op.execute("UPDATE cl_item_files SET submission_id = context_id WHERE context = 'submission'")
    op.execute("UPDATE cl_item_files SET review_log_id = context_id WHERE context = 'review'")
    op.execute("UPDATE cl_item_files SET message_id = context_id WHERE context = 'chat'")

    op.create_foreign_key('cl_item_files_review_log_id_fkey', 'cl_item_files', 'cl_item_reviews_log', ['review_log_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('cl_item_files_message_id_fkey', 'cl_item_files', 'cl_item_messages', ['message_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('cl_item_files_submission_id_fkey', 'cl_item_files', 'cl_item_submissions', ['submission_id'], ['id'], ondelete='SET NULL')
    op.drop_index('ix_cl_item_files_context', table_name='cl_item_files')
    op.drop_column('cl_item_files', 'context_id')
    op.drop_column('cl_item_files', 'context')
