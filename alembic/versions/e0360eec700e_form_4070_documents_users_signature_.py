"""form_4070_documents + users.signature_image_key

Revision ID: e0360eec700e
Revises: 1d29a3cb3a8e
Create Date: 2026-05-14 17:35:01.084188

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e0360eec700e'
down_revision: Union[str, None] = '1d29a3cb3a8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('signature_image_key', sa.String(length=500), nullable=True),
    )
    op.create_table(
        'form_4070_documents',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('employee_id', sa.Uuid(), nullable=False),
        sa.Column('period_id', sa.Uuid(), nullable=False),
        sa.Column('pdf_key', sa.String(length=500), nullable=True),
        sa.Column('reported_cash', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('reported_card', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('paid_out', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('net_tips', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('signature_image_key', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['employee_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['period_id'], ['tip_periods.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('employee_id', 'period_id', name='uq_form_employee_period'),
    )
    op.create_index('ix_form_period', 'form_4070_documents', ['period_id'], unique=False)
    op.create_index(
        'ix_form_employee_status',
        'form_4070_documents',
        ['employee_id', 'status'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_form_employee_status', table_name='form_4070_documents')
    op.drop_index('ix_form_period', table_name='form_4070_documents')
    op.drop_table('form_4070_documents')
    op.drop_column('users', 'signature_image_key')
