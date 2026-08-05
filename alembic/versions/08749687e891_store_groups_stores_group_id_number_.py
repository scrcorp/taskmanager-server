"""store_groups + stores.group_id/number_range_start

매장 그룹(store_groups) 테이블 신설 + stores 에 그룹 FK / 번호대 시작값 추가.

- store_groups.numbering_mode: "group"(그룹 공유 시퀀스, 기본) | "store"(매장별 독립)
- store_groups.number_range_start / stores.number_range_start: empid 번호대 시작값
  (매장 값 > 그룹 값 > 1 순 폴백). 채번 로직은 org_numbering.next_empid.
- 백필 없음: 전부 nullable / server_default. 기존 매장은 미그룹·기본 번호대로 동작 불변.

주의: autogenerate 드리프트(announcements/notifications drop, task 인덱스 등)는 제거하고
이 테이블 + 2컬럼 + 인덱스/FK 만 남긴다 (8d6787f211c0 과 동일 방침).

Revision ID: 08749687e891
Revises: 66760e5e6178
Create Date: 2026-08-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '08749687e891'
down_revision: Union[str, None] = '66760e5e6178'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'store_groups',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('numbering_mode', sa.String(length=10), server_default='group', nullable=False),
        sa.Column('number_range_start', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_store_groups_organization_id'), 'store_groups', ['organization_id'], unique=False)

    op.add_column('stores', sa.Column('group_id', sa.Uuid(), nullable=True))
    op.add_column('stores', sa.Column('number_range_start', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_stores_group_id'), 'stores', ['group_id'], unique=False)
    op.create_foreign_key(
        'fk_stores_group_id_store_groups', 'stores', 'store_groups',
        ['group_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_stores_group_id_store_groups', 'stores', type_='foreignkey')
    op.drop_index(op.f('ix_stores_group_id'), table_name='stores')
    op.drop_column('stores', 'number_range_start')
    op.drop_column('stores', 'group_id')
    op.drop_index(op.f('ix_store_groups_organization_id'), table_name='store_groups')
    op.drop_table('store_groups')
