"""근무 배정 관련 SQLAlchemy ORM 모델 정의.

Work assignment SQLAlchemy ORM model definitions.
Represents daily work assignments linking a user to a specific
brand + shift + position for a given date, with a JSONB snapshot
of the checklist at the time of assignment.

Tables:
    - work_assignments: 근무 배정 (Daily work assignments with checklist snapshots)
"""

import uuid
from datetime import date, datetime, timezone
from sqlalchemy import String, DateTime, Integer, Date, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class WorkAssignment(Base):
    """근무 배정 모델 — 일별 사용자 근무 배정 및 체크리스트 스냅샷.

    Work assignment model — Daily user work assignment with an embedded
    JSONB checklist snapshot. The snapshot captures the checklist template
    items at assignment creation time, making assignments independent of
    future template changes.

    JSONB Snapshot Structure (checklist_snapshot):
        배정 시 ChecklistTemplate의 항목을 복사하여 JSONB로 저장합니다.
        At creation time, items from the matching ChecklistTemplate are
        snapshot-copied into this JSONB column. Each item has:
        [
            {
                "item_index": 0,
                "title": "그릴 예열",
                "description": "400도까지",
                "verification_type": "none",
                "is_completed": false,
                "completed_at": null
            },
            ...
        ]
        직원이 항목을 완료하면 is_completed=true, completed_at=timestamp로 업데이트됩니다.
        (When staff completes an item, is_completed and completed_at are updated in-place.)

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope for multi-tenant isolation)
        brand_id: 브랜드 FK (Brand where the work is performed)
        shift_id: 시간대 FK (Shift period for the assignment)
        position_id: 포지션 FK (Position/station for the assignment)
        user_id: 배정 대상 사용자 FK (Assigned worker)
        work_date: 근무 날짜 (Date of the work assignment)
        status: 진행 상태 (Status: "assigned" -> "in_progress" -> "completed")
        checklist_snapshot: 체크리스트 JSONB 스냅샷 (Frozen copy of checklist items)
        total_items: 총 항목 수 (Total checklist items count)
        completed_items: 완료 항목 수 (Completed items count, auto-incremented)
        assigned_by: 배정자 사용자 FK (Manager/supervisor who created the assignment)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Constraints:
        uq_assignment_combo_date: 동일 날짜에 동일 조합 배정 불가
            (One assignment per brand+shift+position+user+date)
    """

    __tablename__ = "work_assignments"

    # 배정 고유 식별자 — Assignment unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant data isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 브랜드 FK — Brand/store where the work takes place
    brand_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    # 시간대 FK — Shift period (e.g. morning, afternoon)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    # 포지션 FK — Work position/station (e.g. grill, counter)
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False)
    # 배정 대상 사용자 FK — Worker assigned to this task
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 근무 날짜 — Date of the work assignment (date only, no time)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 진행 상태 — Workflow status: "assigned" → "in_progress" → "completed"
    status: Mapped[str] = mapped_column(String(20), default="assigned")  # assigned, in_progress, completed
    # 체크리스트 스냅샷 — JSONB snapshot of checklist items at assignment time (see docstring)
    checklist_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 총 항목 수 — Total number of checklist items (denormalized for quick progress display)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    # 완료 항목 수 — Number of completed items (denormalized, updated on item completion)
    completed_items: Mapped[int] = mapped_column(Integer, default=0)
    # 배정자 FK — Manager/supervisor who created this assignment (nullable for system-generated)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("brand_id", "shift_id", "position_id", "user_id", "work_date", name="uq_assignment_combo_date"),
    )
