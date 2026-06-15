"""tip form 4070 signature_strokes vector

Revision ID: 977ffdc0bc60
Revises: aee9f25302c3
Create Date: 2026-06-12 17:14:51.803929

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '977ffdc0bc60'
down_revision: Union[str, None] = 'aee9f25302c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # form_4070_documents.signature_strokes (벡터 서명 스냅샷) 만 추가한다.
    # (autogenerate 가 모델에 없는 legacy 테이블/인덱스 drop 을 다수 오탐했으나
    #  의도한 변경만 남김 — warning 마이그레이션과 동일한 정리.)
    op.add_column(
        'form_4070_documents',
        sa.Column('signature_strokes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('form_4070_documents', 'signature_strokes')
