"""Attendance Break 모델 — 출퇴근 세션 내 휴식 기록.

Per-attendance break records. Each row represents a single break session
(started_at → ended_at) with a type that determines paid/unpaid classification.

Multiple breaks per attendance are allowed (a staff can take a paid 10min
break + an unpaid meal break in one shift).

Break types (정식):
    - 'paid_10min'  : 10분 유급 짧은 휴식
    - 'unpaid_meal' : 무급 식사 휴식

레거시 값 (paid_short / unpaid_long) 은 dual-read 호환 기간 동안 인식.
backfill migration 완료 후 Phase 3C 에서 제거 예정 (docs NEED_MONITORING.md).
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# 신규 정식 값 — 모든 write 는 normalize_break_type 통과 후 이 값으로 저장.
BREAK_TYPE_PAID_10MIN = "paid_10min"
BREAK_TYPE_UNPAID_MEAL = "unpaid_meal"

# 레거시 값 — read 시 호환 인식 (Phase 3C 에서 삭제).
BREAK_TYPE_PAID_SHORT = "paid_short"
BREAK_TYPE_UNPAID_LONG = "unpaid_long"

# 입력 검증 (신/구 모두 허용 — write 시 normalize 처리).
VALID_BREAK_TYPES = {
    BREAK_TYPE_PAID_10MIN,
    BREAK_TYPE_UNPAID_MEAL,
    BREAK_TYPE_PAID_SHORT,
    BREAK_TYPE_UNPAID_LONG,
}

# paid/unpaid 분류 — aggregation 로직에서 사용.
PAID_BREAK_TYPES = {BREAK_TYPE_PAID_10MIN, BREAK_TYPE_PAID_SHORT}
UNPAID_BREAK_TYPES = {BREAK_TYPE_UNPAID_MEAL, BREAK_TYPE_UNPAID_LONG}


def normalize_break_type(value: str) -> str:
    """레거시 값을 정식 값으로 변환. 정식 값은 그대로 통과."""
    if value == BREAK_TYPE_PAID_SHORT:
        return BREAK_TYPE_PAID_10MIN
    if value == BREAK_TYPE_UNPAID_LONG:
        return BREAK_TYPE_UNPAID_MEAL
    return value


class AttendanceBreak(Base):
    """출퇴근 세션 내 개별 휴식 기록.

    Attributes:
        id: PK UUID
        attendance_id: 부모 attendance FK (CASCADE)
        started_at: 휴식 시작 시각
        ended_at: 휴식 종료 시각 (NULL = in-progress)
        break_type: paid_10min | unpaid_meal (구: paid_short | unpaid_long)
        duration_minutes: 종료 시 계산된 분 단위 길이 (NULL = in-progress)
    """

    __tablename__ = "attendance_breaks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    attendance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("attendances.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    break_type: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_attendance_breaks_attendance_open", "attendance_id", "ended_at"),
    )
