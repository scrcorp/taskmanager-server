"""스케줄 관련 SQLAlchemy ORM 모델 정의.

Schedule-related SQLAlchemy ORM model definitions.

Tables:
    - schedules: 확정 스케줄 (Confirmed schedules — from request confirm or manual creation)
"""

import uuid
from datetime import date, datetime, time, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Date, Time, Text, Boolean, Integer, Numeric, ForeignKey, UniqueConstraint, Index, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StoreWorkRole(Base):
    """매장 업무 역할 — shift+position 조합에 기본시간/휴식/체크리스트 통합."""

    __tablename__ = "store_work_roles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    default_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # Headcount config — always stores all 8 keys: {"all": 3, "sun": 3, "mon": 3, ...}
    # use_per_day_headcount=false → use "all", true → use day keys
    headcount: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {"all": 1, "sun": 1, "mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 1})
    use_per_day_headcount: Mapped[bool] = mapped_column(Boolean, default=False)
    default_checklist_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("checklist_templates.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("store_id", "shift_id", "position_id", name="uq_store_work_role"),
        Index("ix_store_work_roles_store", "store_id"),
    )


class StoreBreakRule(Base):
    """매장 휴게 규칙 — 매장당 1개."""

    __tablename__ = "store_break_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, unique=True)
    max_continuous_minutes: Mapped[int] = mapped_column(Integer, default=240)
    break_duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    max_daily_work_minutes: Mapped[int] = mapped_column(Integer, default=480)
    work_hour_calc_basis: Mapped[str] = mapped_column(String(20), default="per_store")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ScheduleRequestTemplate(Base):
    """스케줄 신청 템플릿 — 직원의 주간 근무 선호도 저장."""

    __tablename__ = "schedule_request_templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_request_templates_user_store", "user_id", "store_id"),
    )


class ScheduleRequestTemplateItem(Base):
    """스케줄 신청 템플릿 항목 — 요일별 선호 근무."""

    __tablename__ = "schedule_request_template_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schedule_request_templates.id", ondelete="CASCADE"), nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Sun, 6=Sat
    work_role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("store_work_roles.id", ondelete="CASCADE"), nullable=False)
    preferred_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    preferred_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    __table_args__ = (
        UniqueConstraint("template_id", "day_of_week", "work_role_id", name="uq_template_day_role"),
    )


class ScheduleRequest(Base):
    """스케줄 신청 — 직원의 근무 신청."""

    __tablename__ = "schedule_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    work_role_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    preferred_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    preferred_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="submitted")  # submitted/accepted/modified/rejected
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    # ─── Original values (stored when SV/GM modifies, for revert) ───
    original_preferred_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    original_preferred_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    original_work_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True)
    original_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    original_work_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 확정 시급 — Effective hourly rate at request creation (auto-filled from user > store > org)
    hourly_rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    # ─── Admin metadata ───
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_schedule_requests_user_date", "user_id", "work_date"),
        Index("ix_schedule_requests_store", "store_id"),
    )


class Schedule(Base):
    """통합 스케줄 — 신청/확정/거절 모든 상태를 포함.

    Status: requested / confirmed / rejected / cancelled
    - requested: staff가 앱에서 신청하거나 admin이 pending으로 생성
    - confirmed: 확정된 근무 스케줄
    - rejected: 거절된 스케줄
    - cancelled: 취소된 스케줄 (confirmed 이후 취소)
    """

    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # Legacy FK — will be removed after full migration from schedule_requests
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("schedule_requests.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    work_role_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    break_start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    break_end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    net_work_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Status: requested / confirmed / rejected / cancelled
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 시급 — auto-filled from user > store > org cascade
    hourly_rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    # Request-specific fields (from merged schedule_requests)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Modification history — JSONB array of {field, old_value, new_value, modified_by, modified_at}
    modifications: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_schedules_org_store_date", "organization_id", "store_id", "work_date"),
        Index("ix_schedules_user_date", "user_id", "work_date"),
        Index("ix_schedules_status", "status"),
    )
