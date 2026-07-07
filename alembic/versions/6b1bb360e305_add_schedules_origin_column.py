"""add schedules.origin column

Revision ID: 6b1bb360e305
Revises: 7adfe9e609ba
Create Date: 2026-06-30 16:43:42.837609

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6b1bb360e305'
down_revision: Union[str, None] = '7adfe9e609ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # schedules.origin — 'manual' (people-created) | 'walk_in' (auto-created on clock-in).
    # NOT NULL with server_default 'manual' so existing rows backfill cleanly.
    # NOTE: autogenerate also flagged unrelated metadata drift (dropped legacy
    # announcement/notification tables, indexes, users.notification_preferences).
    # Those are intentionally NOT included here — this migration is scoped to the
    # walk-in feature's single physical change.
    op.add_column('schedules', sa.Column('origin', sa.String(length=20), server_default='manual', nullable=False))


def downgrade() -> None:
    op.drop_column('schedules', 'origin')
