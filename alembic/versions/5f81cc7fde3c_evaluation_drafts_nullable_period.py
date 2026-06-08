"""evaluation drafts: nullable period

Make evaluations.period_start / period_end nullable so a draft can be saved
partially (only evaluatee required). Submit-time validation still enforces a
valid non-future period in the service layer.

NOTE: autogenerate also flagged unrelated pre-existing model/DB drift
(notifications/announcements tables, several indexes, users.notification_preferences).
Those are out of scope for this change and were intentionally removed — this
migration only touches the two evaluation period columns.

Revision ID: 5f81cc7fde3c
Revises: 3d6dcb5bb447
Create Date: 2026-06-08 18:08:55.956246

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5f81cc7fde3c'
down_revision: Union[str, None] = '3d6dcb5bb447'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'evaluations', 'period_start',
        existing_type=sa.DATE(),
        nullable=True,
    )
    op.alter_column(
        'evaluations', 'period_end',
        existing_type=sa.DATE(),
        nullable=True,
    )


def downgrade() -> None:
    # Re-impose NOT NULL. Rows with NULL period (partial drafts) must be
    # backfilled before downgrading; left to the operator as this is a
    # forward-only relaxation.
    op.alter_column(
        'evaluations', 'period_end',
        existing_type=sa.DATE(),
        nullable=False,
    )
    op.alter_column(
        'evaluations', 'period_start',
        existing_type=sa.DATE(),
        nullable=False,
    )
