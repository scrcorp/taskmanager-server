"""org_members.crewid + org_member_stores.empid (org/store 순번) + 백필

crewid = org 안에서 1부터 순번(org 내 unique). empid = 매장 안에서 1부터 순번(store 내 unique).
기존 행은 created_at 순으로 1..N 백필. 부여 규칙 없이 단순 순번.

주의: autogenerate 드리프트(announcements/notifications drop 등)는 제거하고 이 2컬럼 + 인덱스
+ 백필만 남긴다.

Revision ID: 8d6787f211c0
Revises: cd222bae7dd3
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '8d6787f211c0'
down_revision: Union[str, None] = 'cd222bae7dd3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('org_members', sa.Column('crewid', sa.Integer(), nullable=True))
    op.add_column('org_member_stores', sa.Column('empid', sa.Integer(), nullable=True))

    # 백필 — org별 crewid 1..N (created_at, id 순)
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY organization_id ORDER BY created_at, id) AS rn
            FROM org_members
        )
        UPDATE org_members m SET crewid = ranked.rn
        FROM ranked WHERE m.id = ranked.id AND m.crewid IS NULL
        """
    )
    # 백필 — store별 empid 1..N (created_at, id 순)
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY store_id ORDER BY created_at, id) AS rn
            FROM org_member_stores
        )
        UPDATE org_member_stores s SET empid = ranked.rn
        FROM ranked WHERE s.id = ranked.id AND s.empid IS NULL
        """
    )

    # partial unique 인덱스
    op.create_index(
        'uq_org_member_crewid', 'org_members', ['organization_id', 'crewid'],
        unique=True, postgresql_where=sa.text('crewid IS NOT NULL'),
    )
    op.create_index(
        'uq_org_member_store_empid', 'org_member_stores', ['store_id', 'empid'],
        unique=True, postgresql_where=sa.text('empid IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_org_member_store_empid', table_name='org_member_stores', postgresql_where=sa.text('empid IS NOT NULL'))
    op.drop_index('uq_org_member_crewid', table_name='org_members', postgresql_where=sa.text('crewid IS NOT NULL'))
    op.drop_column('org_member_stores', 'empid')
    op.drop_column('org_members', 'crewid')
