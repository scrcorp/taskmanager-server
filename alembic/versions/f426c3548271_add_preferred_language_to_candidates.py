"""add preferred_language to candidates

Revision ID: f426c3548271
Revises: 443f0c2ed374
Create Date: 2026-05-04 13:26:20.492150

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f426c3548271'
down_revision: Union[str, None] = '443f0c2ed374'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'candidates',
        sa.Column('preferred_language', sa.String(length=8), server_default='en', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('candidates', 'preferred_language')
