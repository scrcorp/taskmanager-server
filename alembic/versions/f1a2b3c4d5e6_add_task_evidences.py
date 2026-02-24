"""add_task_evidences

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-02-24 18:00:00.000000

업무 증빙(task_evidences) 테이블 생성.
추가 업무 완료 시 첨부하는 사진/문서 증빙 데이터를 저장.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # task_evidences — 업무 증빙 (사진/문서 첨부)
    op.create_table(
        'task_evidences',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', UUID(as_uuid=True), sa.ForeignKey('additional_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('file_url', sa.String(500), nullable=False),
        sa.Column('file_type', sa.String(20), server_default='photo'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 인덱스 — task_id 기반 검색 최적화
    op.create_index('ix_task_evidences_task_id', 'task_evidences', ['task_id'])


def downgrade() -> None:
    op.drop_index('ix_task_evidences_task_id', table_name='task_evidences')
    op.drop_table('task_evidences')
