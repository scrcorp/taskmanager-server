"""스태프 근무가능시간(Work Availability) 모델.

각 스태프(user × org)가 요일별로 언제 일할 수 있는지 기록. 상태 3종:
    off   — 안 함 (start/end NULL)
    range — 특정 시간대 (store tz 벽시계). overnight(start>end) 허용, start != end 만 요구.
    full  — 영업일 전체 (start/end NULL)

주는 일요일 시작: day_of_week 0=Sun .. 6=Sat.
⚠️ 이 dow 는 app/utils/timezone.py 의 파이썬 weekday(0=Mon) 헬퍼에 넘기지 말 것.

Tables:
    - staff_availability: (user, org, 요일) 당 1행
    - staff_availability_history: append-only 수정 이력 (누가·언제·무엇·경로)
"""

import uuid
from datetime import datetime, time, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 요일별 가용 상태
AVAILABILITY_STATES = ("off", "range", "full")
# 입력 경로 — 콘솔 매니저 / 스태프 셀프
AVAILABILITY_SOURCES = ("console_manager", "staff_self")


class StaffAvailability(Base):
    """(user, org, 요일) 당 1행. 요일별 근무가능 상태.

    subject = (user_id, organization_id). actor(updated_by) = user.
    행이 없거나 state='off' 이면 그 요일은 근무 불가로 취급.
    """

    __tablename__ = "staff_availability"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 0=Sun .. 6=Sat (Sunday-first). 파이썬 weekday(0=Mon)와 다름 — tz 헬퍼에 넘기지 말 것.
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # off / range / full
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    # store tz 벽시계. range 일 때만 non-null.
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # 마지막 입력 경로 — console_manager / staff_self
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # 마지막 수정자 (actor). 계정 삭제 시 NULL 로 두고 이력은 유지.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "organization_id", "day_of_week",
            name="uq_staff_availability_user_org_dow",
        ),
        CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6",
            name="ck_staff_availability_dow",
        ),
        CheckConstraint(
            "state IN ('off', 'range', 'full')",
            name="ck_staff_availability_state",
        ),
        CheckConstraint(
            "source IN ('console_manager', 'staff_self')",
            name="ck_staff_availability_source",
        ),
        # range → 시간 필수 & start!=end ; off/full → 시간 NULL.
        # overnight(start>end) 허용 — 같은 시각(start==end)만 거부.
        CheckConstraint(
            "(state = 'range' AND start_time IS NOT NULL AND end_time IS NOT NULL"
            " AND end_time <> start_time)"
            " OR (state IN ('off', 'full') AND start_time IS NULL AND end_time IS NULL)",
            name="ck_staff_availability_times",
        ),
    )


class StaffAvailabilityPreset(Base):
    """조직별 근무가능시간 프리셋(기본 세팅). bulk Setup / 개별 편집에서 재사용.

    빌트인 SYSTEM 프리셋은 코드 상수(서비스 계층)로 제공되며 DB 행이 아니다.
    이 테이블은 org 가 만든 커스텀 프리셋만 저장한다 (is_system 은 항상 False).
    days = 7개 요일 스냅샷 JSONB [{day_of_week, state, start_time, end_time}, ...].
    """

    __tablename__ = "staff_availability_presets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 7개 요일 스냅샷 (0=Sun .. 6=Sat)
    days: Mapped[list] = mapped_column(JSONB, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 생성자 (actor). 계정 삭제 시 NULL.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name",
            name="uq_staff_availability_preset_org_name",
        ),
    )


class StaffAvailabilityHistory(Base):
    """append-only 수정 이력. update/delete 하지 않는다 (flush-only append)."""

    __tablename__ = "staff_availability_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 특정 요일 변경 (전체 초기화 등은 NULL)
    day_of_week: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 변경자 (actor)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # 변경 후 상태 스냅샷 {state, start, end}
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 변경 전 상태 (nullable)
    prev: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 사람이 읽는 요약 ("Tue → 09:00–14:30 (was Off)")
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('console_manager', 'staff_self')",
            name="ck_staff_availability_history_source",
        ),
    )
