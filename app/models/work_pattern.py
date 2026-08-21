"""고정 근무(Fixed Schedule) 반복 패턴 모델.

한 사람이 한 매장에서 반복하는 근무 블록을 **별도 원장**으로 보관한다
(SoT: docs/99_inbox/2026-08-20-고정근무-구현계약.md §1-1).

핵심 개념
--------
- **패턴 축 = 사람 × 매장.** `schedules` 행은 실체화(materialize)의 결과일 뿐이고,
  패턴이 삭제되어도 기존 행은 `pattern_id` 가 NULL 로 풀려 일회성 스케줄로 남는다.
- **group_id** — 한 설정창에서 함께 저장한 블록 묶음. 기간 이동·종료·삭제의 단위.
  FK 가 아니라 단순 uuid 다 (그룹 테이블 없음).
- **rrule ↔ byday** — `rrule` 이 RFC 5545 원문(v1 은 `FREQ=WEEKLY;BYDAY=...` 만),
  `byday` 는 그 BYDAY 를 0=Sun..6=Sat 로 투영한 조회·겹침 검사용 컬럼. 서비스가 항상
  둘을 동기화한다. ⚠️ 파이썬 `date.weekday()`(0=Mon) 와 다르다 — 변환 없이 섞지 말 것.
- `(user, store, dow)` 유일성은 **두지 않는다** (블록별 시간이 달라 2교대가 가능).
  겹침은 서비스(`fixed_schedule.validation`)가 검사한다.
- 시간은 store tz 벽시계. overnight(start>end) 허용, `start == end` 만 거부.

Tables:
    - staff_work_patterns: 블록 1개 = 행 1개
"""

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StaffWorkPattern(Base):
    """고정 근무 블록 1개. 같은 설정창에서 저장된 블록은 `group_id` 를 공유한다."""

    __tablename__ = "staff_work_patterns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 패턴 축 = 사람 × 매장
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 한 설정창에서 저장한 블록 묶음 — 기간 이동·종료·삭제의 단위 (FK 아님)
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    work_role_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True
    )
    # RFC 5545 원문. v1 은 WEEKLY 만: "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    rrule: Mapped[str] = mapped_column(Text, nullable=False)
    # rrule 의 BYDAY 투영 — 0=Sun .. 6=Sat. 서비스가 rrule 과 항상 동기.
    byday: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False)
    # store tz 벽시계. overnight(start>end) 허용.
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    break_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL = 무기한
    until_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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
        # overnight 허용 — 같은 시각만 거부
        CheckConstraint("end_time <> start_time", name="ck_staff_work_patterns_times"),
        CheckConstraint(
            "until_date IS NULL OR until_date >= start_date",
            name="ck_staff_work_patterns_period",
        ),
        CheckConstraint("cardinality(byday) > 0", name="ck_staff_work_patterns_byday"),
        # 조회 축 (기간 내 패턴 로드 / 겹침 검사)
        Index("ix_staff_work_patterns_org_store_user", "organization_id", "store_id", "user_id"),
    )
