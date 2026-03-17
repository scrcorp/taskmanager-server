"""체크리스트 관련 SQLAlchemy ORM 모델 정의.

Checklist SQLAlchemy ORM model definitions.
Defines reusable checklist templates scoped to a specific
store + shift + position combination, along with their items,
and checklist instances/items for actual work tracking.

Tables:
    - checklist_templates: 체크리스트 템플릿 (Reusable checklist templates)
    - checklist_template_items: 체크리스트 항목 (Individual items within a template)
    - cl_instances: 체크리스트 인스턴스 (One per schedule)
    - cl_instance_items: 인스턴스 항목 (One per item per instance, snapshot + completion + review)
    - cl_item_files: 첨부파일 (Photos per item, optionally linked to a submission)
    - cl_item_submissions: 제출 이력 (Submission archive per resubmission)
    - cl_item_reviews_log: 리뷰 변경 이력 (Review result change log)
    - cl_item_messages: 메시지 스레드 (Chat-like review messages per item)
"""

import uuid
from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, String, DateTime, Date, Integer, Text, ForeignKey, UniqueConstraint, Uuid, Index
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
    """체크리스트 인스턴스 모델 — 배정 1건당 1개의 체크리스트.

    Checklist instance model — One instance per schedule.
    Items are stored in cl_instance_items (no snapshot JSONB).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope for multi-tenant isolation)
        template_id: 원본 템플릿 FK (Source template, nullable — template may be deleted)
        schedule_id: 스케줄 FK (Schedule, nullable — one instance per schedule)
        store_id: 매장 FK (Store where the work is performed)
        user_id: 배정 대상 사용자 FK (Assigned worker)
        work_date: 근무 날짜 (Date of the work assignment)
        total_items: 총 항목 수 (Total checklist items count)
        completed_items: 완료 항목 수 (Completed items count)
        status: 진행 상태 (Status: "pending" -> "in_progress" -> "completed")
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Constraints:
        schedule_id UNIQUE — one checklist instance per schedule
    """

    __tablename__ = "cl_instances"

    # 인스턴스 고유 식별자 — Instance unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant data isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 원본 템플릿 FK — Source template (SET NULL: 템플릿 삭제 시 null, 아이템은 유지)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("checklist_templates.id", ondelete="SET NULL"), nullable=True)
    # 스케줄 FK — Schedule (SET NULL: 스케줄 삭제 시 null, nullable for migration)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True, unique=True)
    # 매장 FK — Store where the work takes place (SET NULL: 매장 삭제 시 null)
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    # 배정 대상 사용자 FK — Worker assigned to this checklist (SET NULL: 사용자 삭제 시 null)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 근무 날짜 — Date of the work assignment (date only, no time)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
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

    # 관계 — Instance items ordered by item_index
    items = relationship(
        "ChecklistInstanceItem",
        back_populates="instance",
        cascade="all, delete-orphan",
        order_by="ChecklistInstanceItem.item_index",
    )


class ChecklistInstanceItem(Base):
    """체크리스트 인스턴스 항목 — 인스턴스별 항목 1행. 템플릿 스냅샷 + 완료 + 리뷰 통합.

    One row per checklist item per instance.
    Contains template snapshot data (copied at creation),
    completion data, and current review result.
    """

    __tablename__ = "cl_instance_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 인스턴스 FK — Parent instance (CASCADE: 인스턴스 삭제 시 항목도 삭제)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instances.id", ondelete="CASCADE"), nullable=False)
    # 항목 인덱스 — 0-based index within the instance (matches original snapshot order)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # 템플릿 스냅샷 — Copied from template at instance creation time
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_type: Mapped[str] = mapped_column(String(20), default="none")
    min_photos: Mapped[int] = mapped_column(Integer, default=0)
    max_photos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # 완료 데이터 — Completion data (updated when staff completes the item)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_tz: Mapped[str | None] = mapped_column(String(50), nullable=True)
    completed_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # note, location, resubmission_count는 cl_item_submissions에서 관리 (중복 제거)

    # 리뷰 데이터 — Current review result (latest, stored inline for quick read)
    review_result: Mapped[str | None] = mapped_column(String(20), nullable=True)  # pass/fail/caution/pending_re_review
    reviewer_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 생성/수정 일시
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("instance_id", "item_index", name="uq_cl_instance_item_index"),
        Index("ix_cl_instance_items_instance_id", "instance_id"),
    )

    # 관계
    instance = relationship("ChecklistInstance", back_populates="items")
    files = relationship("ChecklistItemFile", back_populates="item", cascade="all, delete-orphan", order_by="ChecklistItemFile.sort_order")
    submissions = relationship("ChecklistItemSubmission", back_populates="item", cascade="all, delete-orphan", order_by="ChecklistItemSubmission.version")
    reviews_log = relationship("ChecklistItemReviewLog", back_populates="item", cascade="all, delete-orphan", order_by="ChecklistItemReviewLog.created_at")
    messages = relationship("ChecklistItemMessage", back_populates="item", cascade="all, delete-orphan", order_by="ChecklistItemMessage.created_at")


class ChecklistItemFile(Base):
    """체크리스트 항목 첨부파일 — 항목별 사진/파일.

    One row per file per instance item.
    Optionally linked to a specific submission (for per-submission photo tracking).
    """

    __tablename__ = "cl_item_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instance_items.id", ondelete="CASCADE"), nullable=False)
    # 어떤 맥락의 파일인지: context + context_id
    context: Mapped[str] = mapped_column(String(20), nullable=False)  # submission | review | chat
    context_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # 해당 submission/review_log/message의 id
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), default="photo")  # photo, video, document
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_cl_item_files_item_id", "item_id"),
        Index("ix_cl_item_files_context", "context", "context_id"),
    )

    item = relationship("ChecklistInstanceItem", back_populates="files")


class ChecklistItemSubmission(Base):
    """체크리스트 항목 제출 이력 — 재제출 시 이전 증거 아카이빙.

    Each resubmission creates a new row with incremented version.
    Version 1 = initial completion, 2+ = resubmissions.
    """

    __tablename__ = "cl_item_submissions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instance_items.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    submitted_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_cl_item_submissions_item_id", "item_id"),
    )

    item = relationship("ChecklistInstanceItem", back_populates="submissions")


class ChecklistItemReviewLog(Base):
    """체크리스트 항목 리뷰 변경 이력 — 리뷰 결과 변경 추적.

    One row per review result change. old_result is null for initial creation.
    """

    __tablename__ = "cl_item_reviews_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instance_items.id", ondelete="CASCADE"), nullable=False)
    old_result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_result: Mapped[str | None] = mapped_column(String(20), nullable=True)  # NULL = review cancelled
    # 리뷰어 코멘트 (X 누르면서 "왼쪽 구석 다시 해" 등)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_cl_item_reviews_log_item_id", "item_id"),
    )

    item = relationship("ChecklistInstanceItem", back_populates="reviews_log")


class ChecklistItemMessage(Base):
    """체크리스트 항목 메시지 — 리뷰 스레드 채팅.

    Chat-like messages per item. Any authorized user can add messages.
    Displayed chronologically as a conversation thread.
    """

    __tablename__ = "cl_item_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cl_instance_items.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 텍스트 본문 (사진만 보내는 경우 NULL)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_cl_item_messages_item_id", "item_id"),
    )

    item = relationship("ChecklistInstanceItem", back_populates="messages")
