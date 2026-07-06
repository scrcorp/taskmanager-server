"""model-b additive: org_members, org_member_stores, platform_admins + users(status, name split, last_org_id)

Model B (전역 정체성) 이행 1단계 — 비파괴 스키마 추가.
새 관계 테이블(org_members / org_member_stores / platform_admins) 생성 +
users 에 이행용 컬럼(status, first/middle/last_name, last_org_id) 추가.
기존 컬럼(organization_id, role_id, full_name, is_active, hourly_rate, ...)은
백필(다음 마이그레이션) + 코드 전환 완료 후 별도 정리 단계에서 제거한다.

주의: autogenerate 가 기존 드리프트(notifications/announcements 미매핑 테이블,
여러 레거시 인덱스, users.notification_preferences)를 drop 하려 했으나 이 이행과
무관하므로 전부 제거하고 의도한 추가 작업만 남겼다.

Revision ID: f8020a1b7740
Revises: 6b1bb360e305
Create Date: 2026-07-02 16:57:10.645887

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f8020a1b7740'
down_revision: Union[str, None] = '6b1bb360e305'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 새 관계 테이블: org_members (user × org 소속) ──────────────────
    op.create_table(
        'org_members',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('role_id', sa.Uuid(), nullable=False),
        sa.Column('hourly_rate', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('department', sa.String(length=20), nullable=True),
        sa.Column('clockin_pin', sa.String(length=6), nullable=True),
        sa.Column('employee_no', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'organization_id', name='uq_org_member_user_org'),
    )
    op.create_index(op.f('ix_org_members_organization_id'), 'org_members', ['organization_id'], unique=False)
    op.create_index(op.f('ix_org_members_user_id'), 'org_members', ['user_id'], unique=False)
    op.create_index('uq_org_member_clockin_pin', 'org_members', ['organization_id', 'clockin_pin'], unique=True, postgresql_where=sa.text('clockin_pin IS NOT NULL'))
    op.create_index('uq_org_member_employee_no', 'org_members', ['organization_id', 'employee_no'], unique=True, postgresql_where=sa.text('employee_no IS NOT NULL'))

    # ── 새 관계 테이블: platform_admins (user × 플랫폼 operator) ────────
    op.create_table(
        'platform_admins',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('level', sa.String(length=20), server_default='super', nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )

    # ── 새 관계 테이블: org_member_stores (org_member × store 매장배정) ─
    op.create_table(
        'org_member_stores',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('org_member_id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=False),
        sa.Column('is_manager', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_work_assignment', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['org_member_id'], ['org_members.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_member_id', 'store_id', name='uq_org_member_store'),
    )
    op.create_index(op.f('ix_org_member_stores_org_member_id'), 'org_member_stores', ['org_member_id'], unique=False)
    op.create_index(op.f('ix_org_member_stores_store_id'), 'org_member_stores', ['store_id'], unique=False)

    # ── users 이행용 컬럼 추가 (비파괴) ────────────────────────────────
    op.add_column('users', sa.Column('last_org_id', sa.Uuid(), nullable=True))
    op.add_column('users', sa.Column('first_name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('middle_name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('last_name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('status', sa.String(length=20), server_default='active', nullable=False))
    op.create_foreign_key('fk_users_last_org', 'users', 'organizations', ['last_org_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_users_last_org', 'users', type_='foreignkey')
    op.drop_column('users', 'status')
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'middle_name')
    op.drop_column('users', 'first_name')
    op.drop_column('users', 'last_org_id')

    op.drop_index(op.f('ix_org_member_stores_store_id'), table_name='org_member_stores')
    op.drop_index(op.f('ix_org_member_stores_org_member_id'), table_name='org_member_stores')
    op.drop_table('org_member_stores')

    op.drop_table('platform_admins')

    op.drop_index('uq_org_member_employee_no', table_name='org_members', postgresql_where=sa.text('employee_no IS NOT NULL'))
    op.drop_index('uq_org_member_clockin_pin', table_name='org_members', postgresql_where=sa.text('clockin_pin IS NOT NULL'))
    op.drop_index(op.f('ix_org_members_user_id'), table_name='org_members')
    op.drop_index(op.f('ix_org_members_organization_id'), table_name='org_members')
    op.drop_table('org_members')
