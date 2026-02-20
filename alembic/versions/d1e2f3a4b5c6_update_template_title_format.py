"""update_checklist_template_title_format

Revision ID: d1e2f3a4b5c6
Revises: a3b4c5d6e7f8
Create Date: 2026-02-20 20:00:00.000000

기존 체크리스트 템플릿 title을 '{shift} - {position}' → '{store} - {shift} - {position}' 형식으로 변환.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # title을 '{store.name} - {shift.name} - {position.name}'으로 일괄 업데이트
    op.execute("""
        UPDATE checklist_templates AS ct
        SET title = s.name || ' - ' || sh.name || ' - ' || p.name,
            updated_at = NOW()
        FROM stores AS s,
             shifts AS sh,
             positions AS p
        WHERE ct.store_id = s.id
          AND ct.shift_id = sh.id
          AND ct.position_id = p.id
    """)


def downgrade() -> None:
    # title을 '{shift.name} - {position.name}'으로 복원
    op.execute("""
        UPDATE checklist_templates AS ct
        SET title = sh.name || ' - ' || p.name,
            updated_at = NOW()
        FROM shifts AS sh,
             positions AS p
        WHERE ct.shift_id = sh.id
          AND ct.position_id = p.id
    """)
