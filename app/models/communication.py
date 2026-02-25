"""커뮤니케이션 관련 SQLAlchemy ORM 모델 정의.

Communication-related SQLAlchemy ORM model definitions.
Includes announcements (org-wide or store-specific notices),
additional tasks (ad-hoc tasks assigned to specific users),
task evidences (photo/document attachments for task completion),
and issue reports (employee-submitted issue tracking).

Tables:
    - announcements: 공지사항 (Organization or store-level announcements)
    - additional_tasks: 추가 업무 (Ad-hoc tasks with priority and assignees)
    - additional_task_assignees: 추가 업무 담당자 (Task-user assignment junction)
    - task_evidences: 업무 증빙 (Photo/document evidence for task completion)
    - issue_reports: 이슈 리포트 (Employee-submitted issue reports)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, ForeignKey, TIMESTAMP, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Announcement(Base):
    """공지사항 모델 — 조직 전체 또는 특정 매장 대상 공지.

    Announcement model — Organization-wide or store-specific notices.
    When store_id is NULL, the announcement targets the entire organization.
    When store_id is set, it targets only users of that store.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope)
        store_id: 대상 매장 FK (Target store, NULL = org-wide announcement)
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
    # 대상 매장 FK — NULL이면 조직 전체 공지 (NULL = org-wide, SET NULL on store delete)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)  # NULL = org-wide
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
        store_id: 대상 매장 FK (Target store, optional)
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
    # 대상 매장 FK — Optional store scope (SET NULL on store delete)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
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
    # 관계 — Evidences (cascade: 업무 삭제 시 증빙도 삭제)
    evidences = relationship("TaskEvidence", back_populates="task", cascade="all, delete-orphan")


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


class TaskEvidence(Base):
    """업무 증빙 모델 — 추가 업무 완료 시 첨부하는 사진/문서 증빙.

    Task evidence model — Photo/document attachments submitted
    by assignees when completing an additional task.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        task_id: 추가 업무 FK (Parent additional task foreign key)
        user_id: 제출자 FK (Submitter user foreign key)
        file_url: 파일 URL (File URL in storage, max 500 chars)
        file_type: 파일 유형 (File type: "photo" or "document")
        note: 메모 (Optional note/description for the evidence)
        created_at: 생성 일시 UTC (Creation timestamp)

    Relationships:
        task: 소속 업무 (Parent additional task)
    """

    __tablename__ = "task_evidences"

    # 증빙 고유 식별자 — Evidence unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 업무 FK — Parent task (CASCADE: 업무 삭제 시 증빙도 삭제)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("additional_tasks.id", ondelete="CASCADE"), nullable=False)
    # 제출자 FK — User who submitted the evidence (CASCADE: 사용자 삭제 시 증빙도 삭제)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 파일 URL — File URL in Supabase Storage (or S3)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # 파일 유형 — "photo" 또는 "document" (File type: photo or document)
    file_type: Mapped[str] = mapped_column(String(20), default="photo")
    # 메모 — Optional note describing the evidence
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships
    task = relationship("AdditionalTask", back_populates="evidences")


class AnnouncementRead(Base):
    """공지사항 읽음 추적 모델 — 사용자별 공지 읽음 기록.

    Announcement read tracking model — Records when a user reads an announcement.
    Used to track read rates and identify unread users.

    Attributes:
        id: 고유 식별자 UUID
        announcement_id: 공지사항 FK
        user_id: 읽은 사용자 FK
        read_at: 읽은 일시 UTC
    """

    __tablename__ = "announcement_reads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    announcement_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class IssueReport(Base):
    """이슈 리포트 모델 — 전 역할 작성 가능한 이슈 보고.

    Issue report model — Issue reports that can be created by any role.
    Tracks issues with status workflow: open → in_progress → resolved.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK
        store_id: 관련 매장 FK (optional)
        title: 이슈 제목
        description: 이슈 상세 설명
        category: 이슈 유형 (facility, equipment, safety, hr, other)
        status: 상태 (open, in_progress, resolved)
        priority: 우선순위 (low, normal, high, urgent)
        created_by: 작성자 FK
        resolved_by: 처리자 FK (optional)
        resolved_at: 처리 완료 일시 (optional)
        created_at: 생성 일시
        updated_at: 수정 일시
    """

    __tablename__ = "issue_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(30), default="other")  # facility, equipment, safety, hr, other
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, in_progress, resolved
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # low, normal, high, urgent
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
