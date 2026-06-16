"""warning wet sign — signature_method + signed_pdf columns

Revision ID: 71b3ba391288
Revises: 026dd7a0ad8f
Create Date: 2026-06-16 09:13:41.658472

wet 서명(출력→실물 서명→PDF 업로드) 지원 컬럼 추가.
- signature_method: 'digital'(기본) | 'wet'. server_default 로 기존 경고 자동 digital.
- signed_pdf_key: wet PDF 상대 key (NULL=미업로드).
- wet_signed_on: 문서상 서명일(date). wet_uploaded_by_id/at: 업로드 audit.

NOTE: autogenerate 무관 변경 오검출 제거, 의도한 컬럼/FK 만 남겼다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '71b3ba391288'
down_revision: Union[str, None] = '026dd7a0ad8f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'warnings',
        sa.Column(
            'signature_method', sa.String(length=10),
            nullable=False, server_default='digital',
        ),
    )
    op.add_column('warnings', sa.Column('signed_pdf_key', sa.String(length=512), nullable=True))
    op.add_column('warnings', sa.Column('wet_signed_on', sa.Date(), nullable=True))
    op.add_column('warnings', sa.Column('wet_uploaded_by_id', sa.Uuid(), nullable=True))
    op.add_column('warnings', sa.Column('wet_uploaded_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        'warnings_wet_uploaded_by_id_fkey',
        'warnings', 'users',
        ['wet_uploaded_by_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('warnings_wet_uploaded_by_id_fkey', 'warnings', type_='foreignkey')
    op.drop_column('warnings', 'wet_uploaded_at')
    op.drop_column('warnings', 'wet_uploaded_by_id')
    op.drop_column('warnings', 'wet_signed_on')
    op.drop_column('warnings', 'signed_pdf_key')
    op.drop_column('warnings', 'signature_method')
