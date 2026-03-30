"""unify headcount into single JSONB

Revision ID: f17bc5d7f162
Revises: 6d47929bfc50
Create Date: 2026-03-30 21:27:11.127079

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f17bc5d7f162'
down_revision: Union[str, None] = '6d47929bfc50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns with defaults
    op.add_column('store_work_roles', sa.Column(
        'headcount',
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default='{"all": 1, "sun": 1, "mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 1}',
    ))
    op.add_column('store_work_roles', sa.Column(
        'use_per_day_headcount', sa.Boolean(), nullable=False, server_default='false',
    ))

    # Migrate data: build JSONB from existing required_headcount + headcount_by_day
    op.execute("""
        UPDATE store_work_roles SET
            headcount = jsonb_build_object(
                'all', required_headcount,
                'sun', COALESCE((headcount_by_day->>'sun')::int, required_headcount),
                'mon', COALESCE((headcount_by_day->>'mon')::int, required_headcount),
                'tue', COALESCE((headcount_by_day->>'tue')::int, required_headcount),
                'wed', COALESCE((headcount_by_day->>'wed')::int, required_headcount),
                'thu', COALESCE((headcount_by_day->>'thu')::int, required_headcount),
                'fri', COALESCE((headcount_by_day->>'fri')::int, required_headcount),
                'sat', COALESCE((headcount_by_day->>'sat')::int, required_headcount)
            ),
            use_per_day_headcount = (headcount_by_day IS NOT NULL)
    """)

    # Drop old columns
    op.drop_column('store_work_roles', 'required_headcount')
    op.drop_column('store_work_roles', 'headcount_by_day')


def downgrade() -> None:
    op.add_column('store_work_roles', sa.Column('headcount_by_day', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('store_work_roles', sa.Column('required_headcount', sa.INTEGER(), server_default=sa.text('1'), nullable=False))
    # Restore from JSONB
    op.execute("""
        UPDATE store_work_roles SET
            required_headcount = COALESCE((headcount->>'all')::int, 1)
    """)
    op.drop_column('store_work_roles', 'use_per_day_headcount')
    op.drop_column('store_work_roles', 'headcount')
