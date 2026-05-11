"""drop uq_user_org_clockin_pin

PIN 인증은 user_id + pin 동시 검증 방식이라 organization 내 unique 강제 불필요.
사용자가 직접 PIN 을 변경할 수 있게 되면서 충돌 회피 부담을 제거한다.

Revision ID: 7130b0a3da7a
Revises: 6f966454bea8
Create Date: 2026-05-11 15:32:58.438555

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7130b0a3da7a'
down_revision: Union[str, None] = '6f966454bea8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('uq_user_org_clockin_pin', 'users', type_='unique')


def downgrade() -> None:
    op.create_unique_constraint(
        'uq_user_org_clockin_pin',
        'users',
        ['organization_id', 'clockin_pin'],
    )
