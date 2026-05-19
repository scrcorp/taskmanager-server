"""add severity to issues

Revision ID: a976607915c3
Revises: 643869a17558
Create Date: 2026-05-13 13:53:06.956715

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a976607915c3'
down_revision: Union[str, None] = '643869a17558'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'issues',
        sa.Column('severity', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('issues', 'severity')
