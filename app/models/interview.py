"""인터뷰 스케줄링 모델 — 매장 가용 슬롯 + 지원자 희망 선호.

설계:
- interview_slots: 매장이 여는 인터뷰 가능 시간. **store-local 벽시계**(slot_date + start/end time, tz 미포함).
  매장 timezone 으로 해석. 확정 시 applications.interview_at(UTC)로 변환 저장.
- interview_slot_preferences: 지원자가 고른 희망 시간 1~3개 (advisory). 확정 전까지 여러 명이 같은 슬롯 선호 가능.
- 확정은 applications.confirmed_slot_id 로 표시 (한 슬롯은 한 application 에만 확정 — partial unique).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.database import Base


class InterviewSlot(Base):
    """조직(org) 단위 인터뷰 가능 시간 한 칸 (org-local 벽시계).

    2026-06-01 결정: 매장별이 아니라 org 통합 — 한 번 세팅하면 모든 매장 지원자가 공유.
    """

    __tablename__ = "interview_slots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # org-local 날짜/시간 (tz 미포함) — org timezone 으로 해석
    slot_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        # org 내 같은 시각 슬롯 중복 방지
        UniqueConstraint("organization_id", "slot_date", "start_time", name="uq_interview_slot_time"),
    )

    organization = relationship("Organization")
    preferences = relationship(
        "InterviewSlotPreference", back_populates="slot", cascade="all, delete-orphan"
    )


class InterviewSlotPreference(Base):
    """지원자가 고른 희망 인터뷰 시간 (application × slot, 1~3개)."""

    __tablename__ = "interview_slot_preferences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("interview_slots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 1~3 선호 순위 (선택). 없어도 됨.
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("application_id", "slot_id", name="uq_pref_application_slot"),
        Index("ix_pref_slot", "slot_id"),
    )

    slot = relationship("InterviewSlot", back_populates="preferences")
    application = relationship("Application")
