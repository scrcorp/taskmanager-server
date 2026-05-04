"""add preferred_language to users

Revision ID: 443f0c2ed374
Revises: a33acbd380c9
Create Date: 2026-05-04 11:26:18.305904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '443f0c2ed374'
down_revision: Union[str, None] = 'a33acbd380c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('preferred_language', sa.String(length=8), server_default='en', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'preferred_language')
