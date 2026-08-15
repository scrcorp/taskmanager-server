"""attendance early thresholds default to 10 minutes

Revision ID: e7b9d2ae5f81
Revises: d1e2l3i4v5r6
Create Date: 2026-08-13 16:49:20.281999

조기 출근/조기 퇴근 임계값의 registry 기본값을 5 → 10 분으로 올리고, 설명 문구를
실제 동작에 맞게 고친다. early clock-in 설명은 "Earlier attempts are rejected" 라고
적혀 있었지만 실제로는 **거부가 아니라 사유 요구**다(사유를 적으면 통과하고 특이사항으로
남는다). 콘솔 설정 화면이 이 문구를 그대로 노출하므로 거짓 설명이 그대로 보인다.

startup seed 는 **신규 키만 insert** 하고 기존 row 는 건드리지 않으므로
(app/main.py::seed_settings_registry), 이미 배포된 환경의 registry default 를 바꾸려면
이 데이터 마이그레이션이 필요하다. 시드 파일(app/seeds/settings_seed.py)과 코드
fallback(app/utils/attendance_judgement.py DEFAULT_*)도 같은 값으로 함께 고쳐져 있다 —
세 곳이 갈리면 매장마다 다르게 동작한다.

org/store 레벨의 명시적 override 는 조직이 직접 정한 값이므로 손대지 않는다.
late_buffer_minutes 는 기본값 5 를 유지하며 설명 문구만 판정 경계를 명시하도록 보강한다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7b9d2ae5f81'
down_revision: Union[str, None] = 'd1e2l3i4v5r6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SQL = sa.text(
    """
    UPDATE settings_registry
       SET default_value = CAST(:value AS jsonb),
           description = :description
     WHERE key = :key
    """
)

_EARLY_CLOCK_IN_KEY = "attendance.early_clock_in_threshold_minutes"
_EARLY_LEAVE_KEY = "attendance.early_leave_threshold_minutes"
_LATE_BUFFER_KEY = "attendance.late_buffer_minutes"

_NEW_EARLY_CLOCK_IN_DESC = (
    "Staff may clock in this many minutes before their shift starts without a reason. "
    "Clocking in earlier asks for a reason and is flagged for the manager to review — "
    "it is never blocked, and there is no limit on how early."
)
_OLD_EARLY_CLOCK_IN_DESC = (
    "How many minutes before a scheduled shift starts staff are allowed to clock in. "
    "Earlier attempts are rejected."
)

_NEW_EARLY_LEAVE_DESC = (
    "Staff may clock out this many minutes before their schedule ends without a reason. "
    "Clocking out earlier asks for a reason and is flagged as 'early leave' — "
    "it is never blocked."
)
_OLD_EARLY_LEAVE_DESC = (
    "Clock-out before this many minutes prior to schedule end is marked as 'early leave'."
)

_NEW_LATE_BUFFER_DESC = (
    "Grace period after schedule start before clock-in is marked as 'late'. "
    "Judged to the minute — with a 5 minute buffer, a 17:00 shift is still on time "
    "at 17:05 and late from 17:06."
)
_OLD_LATE_BUFFER_DESC = (
    "Grace period after schedule start before clock-in is marked as 'late'."
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        _SQL,
        {
            "value": "10",
            "description": _NEW_EARLY_CLOCK_IN_DESC,
            "key": _EARLY_CLOCK_IN_KEY,
        },
    )
    bind.execute(
        _SQL,
        {
            "value": "10",
            "description": _NEW_EARLY_LEAVE_DESC,
            "key": _EARLY_LEAVE_KEY,
        },
    )
    bind.execute(
        _SQL,
        {
            "value": "5",
            "description": _NEW_LATE_BUFFER_DESC,
            "key": _LATE_BUFFER_KEY,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        _SQL,
        {
            "value": "5",
            "description": _OLD_EARLY_CLOCK_IN_DESC,
            "key": _EARLY_CLOCK_IN_KEY,
        },
    )
    bind.execute(
        _SQL,
        {
            "value": "5",
            "description": _OLD_EARLY_LEAVE_DESC,
            "key": _EARLY_LEAVE_KEY,
        },
    )
    bind.execute(
        _SQL,
        {
            "value": "5",
            "description": _OLD_LATE_BUFFER_DESC,
            "key": _LATE_BUFFER_KEY,
        },
    )
