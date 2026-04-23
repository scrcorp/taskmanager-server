"""Attendance Break 모델 — 출퇴근 세션 내 휴식 기록.

Per-attendance break records. Each row represents a single break session
(started_at → ended_at) with a type that determines paid/unpaid classification.

Multiple breaks per attendance are allowed (a staff can take a short paid
break + a long unpaid meal break in one shift).

Break types:
    - 'paid_short'   : 짧은 유급 휴식 (예: 10분)
    - 'unpaid_long'  : 긴 무급 식사 휴식 (예: 30분)

실제 분 단위 기준은 조직/매장 정책에 따라 관리될 수 있으며 현재는 단순 타입 라벨.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


BREAK_TYPE_PAID_SHORT = "paid_short"
BREAK_TYPE_UNPAID_LONG = "unpaid_long"
VALID_BREAK_TYPES = {BREAK_TYPE_PAID_SHORT, BREAK_TYPE_UNPAID_LONG}


class AttendanceBreak(Base):
    """출퇴근 세션 내 개별 휴식 기록.

    Attributes:
        id: PK UUID
        attendance_id: 부모 attendance FK (CASCADE)
        started_at: 휴식 시작 시각
        ended_at: 휴식 종료 시각 (NULL = in-progress)
        break_type: paid_short | unpaid_long
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
