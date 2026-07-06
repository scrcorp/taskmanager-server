"""licenses table (org 1:1) + 기존 org 전부 active 백필

org 운영 자격(license). org 와 1:1. status='suspended'/'expired' 이면 접근 차단.
기존 org 는 모두 active 라이센스로 백필.

주의: autogenerate 드리프트(announcements/notifications drop, 레거시 인덱스 등)는 이 변경과
무관하므로 제거하고 licenses 생성 + 백필만 남긴다.

Revision ID: cd222bae7dd3
Revises: 77271e1d9753
Create Date: 2026-07-03 12:01:13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'cd222bae7dd3'
down_revision: Union[str, None] = '77271e1d9753'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'licenses',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
        sa.Column('plan', sa.String(length=20), server_default='trial', nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id'),
    )
    # 기존 org 전부 active 라이센스 백필
    op.execute(
        """
        INSERT INTO licenses (id, organization_id, status, plan, issued_at, created_at, updated_at)
        SELECT gen_random_uuid(), o.id, 'active', 'trial', now(), now(), now()
        FROM organizations o
        WHERE NOT EXISTS (SELECT 1 FROM licenses l WHERE l.organization_id = o.id)
        """
    )


def downgrade() -> None:
    op.drop_table('licenses')
