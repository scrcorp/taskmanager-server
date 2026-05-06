"""rename notification_preferences to alert_preferences and migrate category codes

Revision ID: 5404355df377
Revises: a6162a07617c
Create Date: 2026-05-06 11:02:22.887626

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5404355df377'
down_revision: Union[str, None] = 'a6162a07617c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 컬럼 rename: users.notification_preferences → users.alert_preferences
    op.execute("ALTER TABLE users RENAME COLUMN notification_preferences TO alert_preferences")

    # JSONB 카테고리 코드 변환: 'announcement' → 'notice'
    op.execute("""
        UPDATE users
        SET alert_preferences = (alert_preferences - 'announcement')
                              || jsonb_build_object('notice', alert_preferences->'announcement')
        WHERE alert_preferences ? 'announcement'
    """)


def downgrade() -> None:
    # 카테고리 코드 역변환: 'notice' → 'announcement'
    op.execute("""
        UPDATE users
        SET alert_preferences = (alert_preferences - 'notice')
                              || jsonb_build_object('announcement', alert_preferences->'notice')
        WHERE alert_preferences ? 'notice'
    """)
    op.execute("ALTER TABLE users RENAME COLUMN alert_preferences TO notification_preferences")
