"""커뮤니케이션 관련 SQLAlchemy ORM 모델 정의.

Communication-related SQLAlchemy ORM model definitions.
Includes announcements (org-wide or brand-specific notices) and
additional tasks (ad-hoc tasks assigned to specific users).

Tables:
    - announcements: 공지사항 (Organization or brand-level announcements)
    - additional_tasks: 추가 업무 (Ad-hoc tasks with priority and assignees)
    - additional_task_assignees: 추가 업무 담당자 (Task-user assignment junction)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, ForeignKey, TIMESTAMP, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Announcement(Base):
    """공지사항 모델 — 조직 전체 또는 특정 브랜드 대상 공지.

    Announcement model — Organization-wide or brand-specific notices.
    When brand_id is NULL, the announcement targets the entire organization.
    When brand_id is set, it targets only users of that brand.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope)
        brand_id: 대상 브랜드 FK (Target brand, NULL = org-wide announcement)
        title: 공지 제목 (Announcement title, max 500 chars)
        content: 공지 내용 (Announcement body text)
        created_by: 작성자 FK (Author user foreign key)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)
    """

    __tablename__ = "announcements"

    # 공지 고유 식별자 — Announcement unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 대상 브랜드 FK — NULL이면 조직 전체 공지 (NULL = org-wide, SET NULL on brand delete)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("brands.id", ondelete="SET NULL"), nullable=True)  # NULL = org-wide
    # 공지 제목 — Announcement title
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 공지 내용 — Announcement body (full text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 작성자 FK — User who created the announcement
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AdditionalTask(Base):
    """추가 업무 모델 — 관리자가 생성하는 임시/추가 업무.

    Additional task model — Ad-hoc tasks created by managers/supervisors.
    Unlike checklist-based work assignments, these are one-off tasks
    with priority levels and optional due dates.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope)
        brand_id: 대상 브랜드 FK (Target brand, optional)
        title: 업무 제목 (Task title)
        description: 업무 상세 설명 (Task description, optional)
        priority: 우선순위 (Priority: "normal" or "urgent")
        status: 진행 상태 (Status: "pending" -> "in_progress" -> "completed")
        due_date: 마감일시 (Due date with timezone, optional)
        created_by: 생성자 FK (Creator user foreign key)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        assignees: 담당자 목록 (List of assigned users, cascade delete)
    """

    __tablename__ = "additional_tasks"

    # 업무 고유 식별자 — Task unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 대상 브랜드 FK — Optional brand scope (SET NULL on brand delete)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("brands.id", ondelete="SET NULL"), nullable=True)
    # 업무 제목 — Task title
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 업무 설명 — Detailed task description (optional)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 우선순위 — Priority level: "normal"(일반) or "urgent"(긴급)
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # normal, urgent
    # 진행 상태 — Workflow status: "pending" → "in_progress" → "completed"
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, in_progress, completed
    # 마감일시 — Optional deadline with timezone (TIMESTAMPTZ)
    due_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # 생성자 FK — Manager/supervisor who created this task
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Assignees (cascade: 업무 삭제 시 담당자 매핑도 삭제)
    assignees = relationship("AdditionalTaskAssignee", back_populates="task", cascade="all, delete-orphan")


class AdditionalTaskAssignee(Base):
    """추가 업무 담당자 모델 — 추가 업무와 사용자 간 다대다 연결 테이블.

    Additional task assignee model — Junction table for the many-to-many
    relationship between AdditionalTask and User. Each row represents
    one user assigned to one task.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        task_id: 업무 FK (Parent task foreign key)
        user_id: 담당자 FK (Assigned user foreign key)
        created_at: 배정 일시 UTC (Assignment timestamp)

    Relationships:
        task: 소속 업무 (Parent additional task)
    """

    __tablename__ = "additional_task_assignees"

    # 매핑 고유 식별자 — Assignee record unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 업무 FK — Parent task (CASCADE: 업무 삭제 시 담당자 매핑도 삭제)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("additional_tasks.id", ondelete="CASCADE"), nullable=False)
    # 담당자 FK — Assigned user (CASCADE: 사용자 삭제 시 매핑도 삭제)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 배정 일시 — When the user was assigned to this task (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships
    task = relationship("AdditionalTask", back_populates="assignees")
