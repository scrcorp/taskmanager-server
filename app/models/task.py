"""Task 모델 — issue_report 에서 promote 또는 직접 생성되는 work item.

명명 변경: 이전엔 Issue 라 불렸지만, "Issue Report"(신고) 와 단어가 겹쳐 혼동되어
Task 로 변경. 테이블도 `tasks` / `task_assignees` 로 rename.

NOTE: legacy `task_evidences` 테이블(`communication.TaskEvidence`)과 이름 충돌을
피하기 위해 evidence 모델은 일단 생략. 향후 필요 시 별도 이름으로 추가.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    TIMESTAMP,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    """Task — 매장 운영 중 발생한 work item.

    source_report_id 가 채워져 있으면 issue_report 에서 promote 된 것.
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # legacy single-store FK — store_ids[0] 와 동기 (backward compat).
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    # task 범위 — store UUID 문자열 list. 빈 array = org-wide (모든 store).
    # 단일 store = [store_id], 여러 store = [s1, s2, ...], org = [].
    store_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    severity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    due_date: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source_report_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    links: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # 첨부 (사진/문서). issue report attachments 와 동일 shape: [{key, mime_type, kind, name, size}].
    # storage_service.resolve_url 로 응답 시 url 보충.
    attachments: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # 담당자가 "Submit for review" 한 시점 (status='submitted' 진입).
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 매니저가 승인/반려한 시점.
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    assignees = relationship(
        "TaskAssignee", back_populates="task", cascade="all, delete-orphan"
    )
    comments = relationship(
        "TaskComment",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskComment.created_at",
    )


class TaskComment(Base):
    """Task 댓글 — 보고/검토 진행 중 메시지 + 첨부 + audit trail.

    담당자가 "Submit for review" 할 때 텍스트 + 사진/영상/파일을 한 묶음으로 보고.
    매니저가 send-back / reopen 할 때도 코멘트만으로 의도 전달.
    """

    __tablename__ = "task_comments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 'comment' (사용자 댓글, 보고 포함) / 'system' (status 전이 기록).
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="comment")
    # 댓글/보고에 첨부된 파일들 — task.attachments 와 동일 shape.
    attachments: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    task = relationship("Task", back_populates="comments")


class TaskAssignee(Base):
    """Task 담당자."""

    __tablename__ = "task_assignees"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("task_id", "user_id", name="uq_task_assignee"),
    )

    task = relationship("Task", back_populates="assignees")
