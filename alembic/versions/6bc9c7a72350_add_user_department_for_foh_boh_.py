"""add user.department for FOH/BOH classification

Revision ID: 6bc9c7a72350
Revises: 10cd6f743384
Create Date: 2026-05-27 13:32:48.030897

직원별 FOH(Front of House)/BOH(Back of House) 분류 컬럼 추가.
값: "FOH" / "BOH" / NULL(미지정). 값 검증은 app schema(Pydantic Literal)에서 수행.

NOTE: autogenerate가 잡은 notifications/announcements 테이블·notification_preferences
컬럼 drop 등은 이 작업과 무관한 기존 모델↔DB 드리프트라 제외함 (이 마이그레이션은
department 추가만 수행).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '6bc9c7a72350'
down_revision: Union[str, None] = '10cd6f743384'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('department', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'department')
