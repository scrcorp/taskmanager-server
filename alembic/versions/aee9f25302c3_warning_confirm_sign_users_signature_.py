"""warning confirm+sign — users.signature_strokes, warnings.acknowledged_at, warning_signatures table

Revision ID: aee9f25302c3
Revises: e482973c572c
Create Date: 2026-06-12 16:28:16.492071

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'aee9f25302c3'
down_revision: Union[str, None] = 'e482973c572c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # warning_signatures 테이블 + users.signature_strokes + warnings.acknowledged_at.
    # (autogenerate 가 모델에 없는 legacy 테이블/인덱스 drop 을 다수 오탐했으나
    #  의도한 변경만 남김 — warning v1.1 마이그레이션과 동일한 정리.)
    op.create_table(
        'warning_signatures',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('warning_id', sa.Uuid(), nullable=False),
        sa.Column('party', sa.String(length=20), nullable=False),
        sa.Column('signer_user_id', sa.Uuid(), nullable=True),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('method', sa.String(length=10), nullable=False),
        sa.Column('signature_strokes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['signer_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['warning_id'], ['warnings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('warning_id', 'party', name='uq_warning_signature_party'),
    )
    op.create_index('ix_warning_signatures_warning_id', 'warning_signatures', ['warning_id'], unique=False)
    op.add_column('users', sa.Column('signature_strokes', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('warnings', sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('warnings', 'acknowledged_at')
    op.drop_column('users', 'signature_strokes')
    op.drop_index('ix_warning_signatures_warning_id', table_name='warning_signatures')
    op.drop_table('warning_signatures')
