"""username global unique (drop per-org, add global)

Revision ID: 832ddded650f
Revises: fa6e1f81bfb7
Create Date: 2026-07-06 11:03:54.690874

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '832ddded650f'
down_revision: Union[str, None] = 'fa6e1f81bfb7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Model B: username = 전역 로그인 아이디 → per-org 유니크(org, username)를 전역 유니크로.
    # (로그인이 company_code 없을 때 username 전역 조회 → 중복이면 깨짐. 전역 유니크 필수.)
    op.drop_constraint('uq_user_org_username', 'users', type_='unique')
    op.create_unique_constraint('uq_user_username', 'users', ['username'])


def downgrade() -> None:
    op.drop_constraint('uq_user_username', 'users', type_='unique')
    op.create_unique_constraint('uq_user_org_username', 'users', ['organization_id', 'username'])
