"""add staff_availability_presets

Revision ID: 66760e5e6178
Revises: 1abb4bb59226
Create Date: 2026-07-13 18:19:06.038539

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '66760e5e6178'
down_revision: Union[str, None] = '1abb4bb59226'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('staff_availability_presets',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('days', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('is_system', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'name', name='uq_staff_availability_preset_org_name')
    )
    op.create_index(op.f('ix_staff_availability_presets_organization_id'), 'staff_availability_presets', ['organization_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_staff_availability_presets_organization_id'), table_name='staff_availability_presets')
    op.drop_table('staff_availability_presets')
