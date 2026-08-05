"""add clockin_pin_audits

Revision ID: fefe9c6e1492
Revises: 08749687e891
Create Date: 2026-08-03 20:02:03.673753

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'fefe9c6e1492'
down_revision: Union[str, None] = '08749687e891'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # autogenerate 가 잡아낸 기존 드리프트(무관한 테이블/인덱스 삭제)는 모두 제거했다.
    # 이 마이그레이션은 clockin_pin_audits 생성만 한다.
    op.create_table(
        'clockin_pin_audits',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('actor_user_id', sa.Uuid(), nullable=False),
        sa.Column('target_user_id', sa.Uuid(), nullable=False),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('device_id', sa.Uuid(), nullable=True),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['device_id'], ['attendance_devices.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['target_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clockin_pin_audits_actor', 'clockin_pin_audits', ['actor_user_id', 'created_at'], unique=False)
    op.create_index('ix_clockin_pin_audits_org', 'clockin_pin_audits', ['organization_id', 'created_at'], unique=False)
    op.create_index('ix_clockin_pin_audits_target', 'clockin_pin_audits', ['target_user_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_clockin_pin_audits_target', table_name='clockin_pin_audits')
    op.drop_index('ix_clockin_pin_audits_org', table_name='clockin_pin_audits')
    op.drop_index('ix_clockin_pin_audits_actor', table_name='clockin_pin_audits')
    op.drop_table('clockin_pin_audits')
