"""add access_code_audits

Revision ID: 86e542e71dac
Revises: 28f55a28c9f4
Create Date: 2026-08-07 19:40:02.504528

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '86e542e71dac'
down_revision: Union[str, None] = '28f55a28c9f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('access_code_audits',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('service_key', sa.String(length=50), nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_access_code_audits_org', 'access_code_audits', ['organization_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_access_code_audits_org', table_name='access_code_audits')
    op.drop_table('access_code_audits')
