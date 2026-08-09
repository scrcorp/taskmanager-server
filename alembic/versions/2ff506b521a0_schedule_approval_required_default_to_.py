"""schedule.approval_required default to false (SV can manage confirmed schedules)

Revision ID: 2ff506b521a0
Revises: 7250b506159c
Create Date: 2026-08-08 19:58:40.269510

승인 워크플로를 기본 OFF 로 전환. SV 가 매장 실무를 돌리는 구조라 confirmed 스케줄
생성·수정·삭제를 GM 승인 없이 할 수 있는 것이 기본값이고, 승인 절차가 필요한 조직만
이 설정을 켠다.

startup seed 는 **신규 키만 insert** 하고 기존 row 는 건드리지 않으므로, 이미 배포된
환경의 registry default 를 바꾸려면 이 데이터 마이그레이션이 필요하다.
org/store 레벨의 명시적 override 는 조직이 직접 정한 값이므로 손대지 않는다 —
켜 둔 조직은 그대로 승인 워크플로가 유지된다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '2ff506b521a0'
down_revision: Union[str, None] = '7250b506159c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KEY = "schedule.approval_required"
_NEW_DESCRIPTION = (
    "When on, schedules created by SV are submitted as requests and need GM confirmation, "
    "and only GM+ can edit or delete confirmed schedules. "
    "Off by default — SV runs the store day to day and creates confirmed schedules directly."
)
_OLD_DESCRIPTION = "All requested schedules need GM confirmation before becoming active."

_SQL = sa.text(
    """
    UPDATE settings_registry
       SET default_value = CAST(:value AS jsonb),
           description = :description
     WHERE key = :key
    """
)


def upgrade() -> None:
    op.get_bind().execute(
        _SQL, {"value": "false", "description": _NEW_DESCRIPTION, "key": _KEY}
    )


def downgrade() -> None:
    op.get_bind().execute(
        _SQL, {"value": "true", "description": _OLD_DESCRIPTION, "key": _KEY}
    )
