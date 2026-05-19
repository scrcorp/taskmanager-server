"""task attachments comments submitted reviewed

Revision ID: 3e96032d6c76
Revises: c71d3411482f
Create Date: 2026-05-18 19:00:49.841874

변경 사항:
- tasks 테이블에 attachments JSONB / submitted_at / submitted_by / reviewed_at / reviewed_by 컬럼 추가
- task_comments 테이블 신설 (id, task_id, user_id, content, kind, created_at)

NOTE: 다른 unrelated drift (announcements / notifications / users.notification_preferences /
인덱스 이름 변경 등) 는 이 migration 에서 다루지 않음.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '3e96032d6c76'
down_revision: Union[str, None] = 'c71d3411482f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── task_comments 테이블 신설 ──────────────────────────────────────
    op.create_table(
        'task_comments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='comment'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_task_comments_task_created',
        'task_comments',
        ['task_id', 'created_at'],
        unique=False,
    )

    # ── tasks 컬럼 추가 ────────────────────────────────────────────────
    op.add_column(
        'tasks',
        sa.Column(
            'attachments',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='[]',
            nullable=False,
        ),
    )
    op.add_column(
        'tasks',
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'tasks',
        sa.Column('submitted_by', sa.Uuid(), nullable=True),
    )
    op.add_column(
        'tasks',
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'tasks',
        sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'tasks_submitted_by_fkey',
        'tasks',
        'users',
        ['submitted_by'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'tasks_reviewed_by_fkey',
        'tasks',
        'users',
        ['reviewed_by'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('tasks_reviewed_by_fkey', 'tasks', type_='foreignkey')
    op.drop_constraint('tasks_submitted_by_fkey', 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'reviewed_by')
    op.drop_column('tasks', 'reviewed_at')
    op.drop_column('tasks', 'submitted_by')
    op.drop_column('tasks', 'submitted_at')
    op.drop_column('tasks', 'attachments')

    op.drop_index('ix_task_comments_task_created', table_name='task_comments')
    op.drop_table('task_comments')
