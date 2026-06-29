"""merge employee_no ledger + daily-report heads

Revision ID: 4ae194c4f826
Revises: 0c71a637c93f, d6bcf1528c63
Create Date: 2026-06-29 17:30:23.658534

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ae194c4f826'
down_revision: Union[str, None] = ('0c71a637c93f', 'd6bcf1528c63')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
