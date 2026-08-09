"""attendance timeline: group_id/action/target columns

Activity History 정비 — 이력 행이 "무엇의 전이인가"를 표현할 수 있게 한다.

- group_id:    한 사용자 액션이 만든 행들을 묶는 키 (콘솔이 카드 하나로 렌더).
               기존 "같은 actor + 2초 이내" 휴리스틱을 대체한다.
- action:      카드 태그 — 무엇을 했나 (clock_in / modify / break_added …)
- target_type: 전이 대상 엔터티 — "attendance" | "break"
- target_id:   하위 엔터티 식별자 (break 세션 id). FK 를 걸지 않는다 —
               세션이 삭제돼도 "삭제했다"는 이력은 남아야 하기 때문.

전부 nullable. 기존 행은 백필하지 않는다 (역산은 추측이라 감사 이력을 오염시킨다).
콘솔은 이 값들이 NULL 이면 레거시 행으로 보고 기존 방식으로 fallback 한다.

Revision ID: b075fd4d7262
Revises: 2ff506b521a0
Create Date: 2026-08-09 10:54:57.170056

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b075fd4d7262'
down_revision: Union[str, None] = '2ff506b521a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "attendance_corrections",
        sa.Column("group_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "attendance_corrections",
        sa.Column("action", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "attendance_corrections",
        sa.Column("target_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "attendance_corrections",
        sa.Column("target_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attendance_corrections", "target_id")
    op.drop_column("attendance_corrections", "target_type")
    op.drop_column("attendance_corrections", "action")
    op.drop_column("attendance_corrections", "group_id")
