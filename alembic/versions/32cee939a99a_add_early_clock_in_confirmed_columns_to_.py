"""add early_clock_in_confirmed columns to attendances

조기 출근 강행(early_clock_in_override) 건을 매니저가 확인했는지 기록한다.
payroll 마감 게이트가 이 값으로 미확인 건을 판정한다 (자동퇴근 확인과 같은 모양).

Revision ID: 32cee939a99a
Revises: b075fd4d7262
Create Date: 2026-08-09 13:12:03.581893

주의: 이 프로젝트의 DB 는 모델과 드리프트가 있어 `--autogenerate` 결과에
무관한 drop_table/drop_index 가 대량으로 섞여 나온다. 이 파일은 이번 변경분
(컬럼 2개 + FK) 만 남기고 손으로 정리한 것이다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '32cee939a99a'
down_revision: Union[str, None] = 'b075fd4d7262'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'attendances',
        sa.Column('early_clock_in_confirmed_by', sa.Uuid(), nullable=True),
    )
    op.add_column(
        'attendances',
        sa.Column(
            'early_clock_in_confirmed_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        'fk_attendances_early_clock_in_confirmed_by_users',
        'attendances',
        'users',
        ['early_clock_in_confirmed_by'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_attendances_early_clock_in_confirmed_by_users',
        'attendances',
        type_='foreignkey',
    )
    op.drop_column('attendances', 'early_clock_in_confirmed_at')
    op.drop_column('attendances', 'early_clock_in_confirmed_by')
