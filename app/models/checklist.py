"""체크리스트 관련 SQLAlchemy ORM 모델 정의.

Checklist SQLAlchemy ORM model definitions.
Defines reusable checklist templates scoped to a specific
store + shift + position combination, along with their items,
and checklist instances/completions for actual work tracking.

Tables:
    - checklist_templates: 체크리스트 템플릿 (Reusable checklist templates)
    - checklist_template_items: 체크리스트 항목 (Individual items within a template)
    - cl_instances: 체크리스트 인스턴스 (One per work assignment, snapshot of template)
    - cl_completions: 체크리스트 완료 기록 (One per completed item in an instance)
"""

import uuid
from datetime import date, datetime, timezone
from sqlalchemy import String, DateTime, Date, Integer, Text, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ChecklistTemplate(Base):
    """체크리스트 템플릿 모델 — 매장/시간대/포지션 조합별 업무 체크리스트.

    Checklist template model — Reusable task checklist for a specific
    store + shift + position combination. Only one template can exist
    per combination (enforced by unique constraint).

    When a WorkAssignment is created, the template's items are
    snapshot-copied into the assignment's JSONB checklist_snapshot field.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        store_id: 소속 매장 FK (Store foreign key)
        shift_id: 시간대 FK (Shift foreign key)
        position_id: 포지션 FK (Position foreign key)
        title: 템플릿 제목 (Template title)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        items: 체크리스트 항목 목록 (Template items, ordered by sort_order)

    Constraints:
        uq_template_store_shift_position: 매장+시간대+포지션 조합 고유 (One template per combination)

    Note:
        recurrence는 item 레벨로 이동됨 (recurrence moved to item level)
    """

    __tablename__ = "checklist_templates"

    # 템플릿 고유 식별자 — Template unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 매장 FK — Store scope (CASCADE: 매장 삭제 시 템플릿도 삭제)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 시간대 FK — Shift scope (CASCADE: 시간대 삭제 시 템플릿도 삭제)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    # 포지션 FK — Position scope (CASCADE: 포지션 삭제 시 템플릿도 삭제)
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False)
    # 템플릿 제목 — Template display title
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("store_id", "shift_id", "position_id", name="uq_template_store_shift_position"),
    )

    # 관계 — Items sorted by sort_order for consistent display ordering
    items = relationship("ChecklistTemplateItem", back_populates="template", cascade="all, delete-orphan", order_by="ChecklistTemplateItem.sort_order")
    # 관계 — Shift and Position for name lookups
    shift = relationship("Shift", foreign_keys=[shift_id], lazy="noload")
    position = relationship("Position", foreign_keys=[position_id], lazy="noload")


class ChecklistTemplateItem(Base):
    """체크리스트 템플릿 항목 모델 — 개별 체크리스트 작업 항목.

    Checklist template item model — Individual task item within a template.
    Items are ordered by sort_order and can require different verification types.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        template_id: 소속 템플릿 FK (Parent template foreign key)
        title: 항목 제목 (Item title/task description)
        description: 상세 설명 (Detailed description, optional)
        verification_type: 확인 유형 (Verification method: "none", "photo", "text")
        recurrence_type: 반복 주기 유형 ("daily"=매일, "weekly"=특정 요일만)
        recurrence_days: 반복 요일 목록 (weekly일 때 [0=Mon..6=Sun])
        sort_order: 정렬 순서 (Display order, lower = first)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        template: 소속 템플릿 (Parent checklist template)
    """

    __tablename__ = "checklist_template_items"

    # 항목 고유 식별자 — Item unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 템플릿 FK — Parent template (CASCADE: 템플릿 삭제 시 항목도 삭제)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("checklist_templates.id", ondelete="CASCADE"), nullable=False)
    # 항목 제목 — Task title/description (max 500 chars)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 상세 설명 — Optional detailed instructions for the task
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 확인 유형 — Verification method: "none"=체크만, "photo"=사진첨부, "text"=텍스트입력
    verification_type: Mapped[str] = mapped_column(String(20), default="none")  # none, photo, text
    # 반복 주기 유형 — "daily"=매일, "weekly"=특정 요일만
    recurrence_type: Mapped[str] = mapped_column(String(10), default="daily", nullable=False)
    # 반복 요일 목록 — weekly일 때 요일 숫자 배열 [0=Mon..6=Sun]. daily이면 null
    recurrence_days: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True, default=None)
    # 정렬 순서 — Display sort order (0-based, supports drag-and-drop reordering)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships
    template = relationship("ChecklistTemplate", back_populates="items")


class ChecklistInstance(Base):
    """체크리스트 인스턴스 모델 — 배정 1건당 1개의 체크리스트 스냅샷.

    Checklist instance model — One instance per work assignment.
    Stores a frozen snapshot of template items at assignment creation time,
    allowing templates to change without affecting existing assignments.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope for multi-tenant isolation)
        template_id: 원본 템플릿 FK (Source template, nullable — template may be deleted)
        work_assignment_id: 근무 배정 FK (Work assignment, UNIQUE — one instance per assignment)
        store_id: 매장 FK (Store where the work is performed)
        user_id: 배정 대상 사용자 FK (Assigned worker)
        work_date: 근무 날짜 (Date of the work assignment)
        snapshot: JSONB 스냅샷 (Frozen copy of template items at creation time)
        total_items: 총 항목 수 (Total checklist items count)
        completed_items: 완료 항목 수 (Completed items count)
        status: 진행 상태 (Status: "pending" -> "in_progress" -> "completed")
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        completions: 완료 기록 목록 (Completion records for this instance)

    Constraints:
        work_assignment_id UNIQUE — one checklist instance per assignment
    """

    __tablename__ = "cl_instances"

    # 인스턴스 고유 식별자 — Instance unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant data isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 원본 템플릿 FK — Source template (SET NULL: 템플릿 삭제 시 null, 스냅샷은 유지)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("checklist_templates.id", ondelete="SET NULL"), nullable=True)
    # 근무 배정 FK — Work assignment (CASCADE: 배정 삭제 시 인스턴스도 삭제, UNIQUE)
    work_assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("work_assignments.id", ondelete="CASCADE"), nullable=False, unique=True)
    # 매장 FK — Store where the work takes place
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 배정 대상 사용자 FK — Worker assigned to this checklist
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 근무 날짜 — Date of the work assignment (date only, no time)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    # JSONB 스냅샷 — Frozen copy of template items at instance creation time
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 총 항목 수 — Total number of checklist items (denormalized for quick progress display)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    # 완료 항목 수 — Number of completed items (denormalized, updated on item completion)
    completed_items: Mapped[int] = mapped_column(Integer, default=0)
    # 진행 상태 — Workflow status: "pending" → "in_progress" → "completed"
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Completion records ordered by item_index
    completions = relationship("ChecklistCompletion", back_populates="instance", cascade="all, delete-orphan", order_by="ChecklistCompletion.item_index")


class ChecklistCompletion(Base):
    """체크리스트 완료 기록 모델 — 인스턴스 내 개별 항목 완료 기록.

    Checklist completion model — One row per completed item in an instance.
    Records who completed an item, when, and optional evidence (photo, note, GPS).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        instance_id: 소속 인스턴스 FK (Parent checklist instance)
        item_index: 항목 인덱스 (Matches snapshot item_index)
        user_id: 완료한 사용자 FK (User who completed the item)
        completed_at: 완료 일시 (Completion timestamp)
        photo_url: 사진 URL (Photo evidence URL, optional)
        note: 메모 (Text note, optional)
        location: GPS 위치 JSONB (lat/lng, optional)
        created_at: 생성 일시 UTC (Creation timestamp)

    Constraints:
        uq_cl_completion_instance_item: (instance_id, item_index) — one completion per item
    """

    __tablename__ = "cl_completions"

    # 완료 기록 고유 식별자 — Completion record unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 인스턴스 FK — Parent instance (CASCADE: 인스턴스 삭제 시 완료 기록도 삭제)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instances.id", ondelete="CASCADE"), nullable=False)
    # 항목 인덱스 — Matches snapshot item_index (0-based)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 완료한 사용자 FK — User who completed this item
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 완료 일시 — When the item was completed (UTC with timezone)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # IANA 타임존 — Timezone at completion (e.g. "America/Los_Angeles") for local time display
    completed_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 사진 URL — Optional photo evidence URL (Supabase Storage)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 메모 — Optional text note for the completion
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # GPS 위치 — Optional location data as JSONB {lat, lng}
    location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("instance_id", "item_index", name="uq_cl_completion_instance_item"),
    )

    # 관계 — Parent instance
    instance = relationship("ChecklistInstance", back_populates="completions")


class ChecklistComment(Base):
    """체크리스트 코멘트 모델 — 인스턴스에 대한 코멘트/메모.

    Checklist comment model — Comments on a checklist instance.
    Allows managers and staff to leave notes on checklist progress.

    Attributes:
        id: 고유 식별자 UUID
        instance_id: 소속 인스턴스 FK
        user_id: 작성자 FK
        text: 코멘트 내용
        created_at: 생성 일시 UTC
    """

    __tablename__ = "cl_comments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instances.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    instance = relationship("ChecklistInstance", foreign_keys=[instance_id])
