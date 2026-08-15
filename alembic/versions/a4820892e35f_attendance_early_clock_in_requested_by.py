"""attendance early_clock_in_requested_by

조기 출근 **요청자**(= "누가 일찍 오라고 했나") 를 식별자로 남긴다 (D9).
표시용 문자열은 지금처럼 attendance_corrections.reason 에 남고, 이 컬럼은 식별 전용이다.
"직접 입력(Someone else)" 과 구버전 HTMA 는 값을 보내지 않으므로 NULL 이 정상.

add_column + FK 하나뿐 — drop_* 이 없다.

Revision ID: a4820892e35f
Revises: e7b9d2ae5f81
Create Date: 2026-08-13 20:07:53.212993

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4820892e35f'
down_revision: Union[str, None] = 'e7b9d2ae5f81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 이름을 명시한다 — autogenerate 가 넣은 None 이면 downgrade 의 drop_constraint 가
# "어느 제약인지" 를 못 찾아 실패한다.
FK_NAME = "fk_attendances_early_clock_in_requested_by_users"


def upgrade() -> None:
    op.add_column(
        'attendances',
        sa.Column('early_clock_in_requested_by', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        FK_NAME,
        'attendances',
        'users',
        ['early_clock_in_requested_by'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(FK_NAME, 'attendances', type_='foreignkey')
    op.drop_column('attendances', 'early_clock_in_requested_by')
