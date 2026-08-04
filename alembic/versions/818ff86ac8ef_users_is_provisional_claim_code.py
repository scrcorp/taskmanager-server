"""users.is_provisional + claim_code

미가입(유령) 직원 계정 — 관리자가 미리 만들어 두고 empid·스케줄을 붙일 수 있는 자리.

- is_provisional: 유령 표식. 반드시 is_active=False 와 함께 쓴다(fail-closed).
  로그인·PIN·알림·팁·리포트는 is_active 게이트로 자동 차단되고, 스케줄 후보에만 명시 포함.
- claim_code: 본인이 가입할 때 이 행을 인수하는 코드. 인수 완료 시 NULL 로 반납
  (org 내 non-null partial unique — 반납된 코드는 재사용 가능).

백필 없음: 기존 유저는 전부 is_provisional=false(server_default), claim_code NULL.

주의: autogenerate 드리프트(announcements/notifications drop, 각종 인덱스 drop 등)는 제거하고
이 2컬럼 + 인덱스 2개만 남긴다 (08749687e891 과 동일 방침).

Revision ID: 818ff86ac8ef
Revises: 08749687e891
Create Date: 2026-08-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '818ff86ac8ef'
down_revision: Union[str, None] = '08749687e891'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('is_provisional', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('users', sa.Column('claim_code', sa.String(length=12), nullable=True))
    op.create_index(op.f('ix_users_is_provisional'), 'users', ['is_provisional'], unique=False)
    op.create_index(
        'uq_user_org_claim_code', 'users', ['organization_id', 'claim_code'],
        unique=True, postgresql_where=sa.text('claim_code IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_user_org_claim_code', table_name='users', postgresql_where=sa.text('claim_code IS NOT NULL'))
    op.drop_index(op.f('ix_users_is_provisional'), table_name='users')
    op.drop_column('users', 'claim_code')
    op.drop_column('users', 'is_provisional')
