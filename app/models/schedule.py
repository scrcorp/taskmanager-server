"""스케줄 관련 SQLAlchemy ORM 모델 정의.

Schedule-related SQLAlchemy ORM model definitions.
Represents schedule drafts created by supervisors, reviewed/approved by
general managers, with an audit trail for approval actions.

Tables:
    - schedules: 스케줄 초안 (Schedule drafts — SV creates, GM approves)
    - schedule_approvals: 승인 이력 (Approval audit trail)
"""

import uuid
from datetime import date, datetime, time, timezone
from sqlalchemy import String, DateTime, Date, Time, Text, Boolean, ForeignKey, UniqueConstraint, Index, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Schedule(Base):
    """스케줄 모델 — SV가 작성하고 GM이 승인하는 근무 스케줄 초안.

    Schedule model — Draft work schedule created by Supervisor,
    reviewed and approved by General Manager. Upon approval,
    a work_assignment row is automatically created.

    Status Flow:
        draft → pending → approved → (optional) cancelled
        - draft: SV가 작성 중 (SV is drafting)
        - pending: 승인 요청됨 (Submitted for approval)
        - approved: GM이 승인, work_assignment 자동 생성 (GM approved, work_assignment auto-created)
        - cancelled: 취소됨 (Cancelled before approval)

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope for multi-tenant isolation)
        store_id: 매장 FK (Store where the work is scheduled)
        user_id: 배정 대상 직원 FK (Target employee for this schedule)
        shift_id: 시간대 FK, 선택 (Optional shift period)
        position_id: 포지션 FK, 선택 (Optional position/station)
        work_date: 근무 날짜 (Scheduled work date)
        start_time: 시작 시각, 선택 (Start time, optional if shift provides it)
        end_time: 종료 시각, 선택 (End time, optional if shift provides it)
        status: 상태 (Status: draft/pending/approved/cancelled)
        note: 메모 (Optional note)
        created_by: 작성자 FK — SV (Creator, usually Supervisor)
        approved_by: 승인자 FK — GM (Approver, usually General Manager)
        approved_at: 승인 일시 (Approval timestamp)
        work_assignment_id: 승인 후 생성된 배정 FK (Work assignment created upon approval)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Constraints:
        uq_schedule_user_store_date_shift: 동일 사용자+매장+날짜+시프트 중복 방지
            (One schedule per user+store+date+shift combination)
    """

    __tablename__ = "schedules"

    # 스케줄 고유 식별자 — Schedule unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant data isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 매장 FK — Store where the work is scheduled
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 배정 대상 직원 FK — Target employee for the schedule
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 시간대 FK — Shift period (optional, nullable)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True)
    # 포지션 FK — Position/station (optional, nullable)
    position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    # 근무 날짜 — Scheduled work date (date only, no time)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 시작 시각 — Start time (optional; shift에서 가져오거나 직접 입력)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # 종료 시각 — End time (optional; shift에서 가져오거나 직접 입력)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # 상태 — Status: "draft" → "pending" → "approved" → "cancelled"
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # 메모 — Optional note
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 작성자 FK — Creator (usually Supervisor)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 승인자 FK — Approver (usually General Manager)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 승인 일시 — Approval timestamp (UTC)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 승인 후 생성된 배정 FK — Work assignment created upon approval
    work_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_assignments.id", ondelete="SET NULL"), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "store_id", "work_date", "shift_id", name="uq_schedule_user_store_date_shift"),
        Index("ix_schedules_org_store_date", "organization_id", "store_id", "work_date"),
        Index("ix_schedules_user_date", "user_id", "work_date"),
    )


class ScheduleApproval(Base):
    """스케줄 승인 이력 모델 — 승인/반려 액션의 감사 추적 기록.

    Schedule approval audit trail model — Records each approval or
    rejection action for audit compliance and history tracking.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        schedule_id: 스케줄 FK (Target schedule)
        action: 액션 유형 — "approve" / "reject" (future) (Action type)
        user_id: 액션 수행자 FK (User who performed the action)
        reason: 사유, 반려 시 사용 (Reason, used for rejection — future)
        created_at: 생성 일시 UTC (Action timestamp)
    """

    __tablename__ = "schedule_approvals"

    # 승인 이력 고유 식별자 — Approval record unique identifier (UUID v4)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 스케줄 FK — Target schedule (CASCADE: 스케줄 삭제 시 이력도 삭제)
    schedule_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    # 액션 유형 — "approve" or "reject" (future)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    # 액션 수행자 FK — User who performed the action
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 사유 — Reason for rejection (future feature)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 생성 일시 — Action timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
