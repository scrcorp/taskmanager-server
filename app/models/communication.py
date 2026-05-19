"""커뮤니케이션 관련 SQLAlchemy ORM 모델 정의.

Communication-related SQLAlchemy ORM model definitions.
Includes notices (org-wide or store-specific notices),
additional tasks (ad-hoc tasks assigned to specific users),
task evidences (photo/document attachments for task completion),
and voices (employee-submitted ideas/suggestions/issues).

Tables:
    - notices: 공지사항 (Organization or store-level notices)
    - additional_tasks: 추가 업무 (Ad-hoc tasks with priority and assignees)
    - additional_task_assignees: 추가 업무 담당자 (Task-user assignment junction)
    - task_evidences: 업무 증빙 (Photo/document evidence for task completion)
    - voices: Voices (Employee-submitted ideas, suggestions, and issues)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Text, ForeignKey, TIMESTAMP, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Notice(Base):
    """공지 모델 — 조직 전체 또는 특정 매장 대상 공지.

    Notice model — Organization-wide or store-specific notices.
    When store_id is NULL, the notice targets the entire organization.
    When store_id is set, it targets only users of that store.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope)
        store_id: 대상 매장 FK (Target store, NULL = org-wide notice)
        title: 공지 제목 (Notice title, max 500 chars)
        content: 공지 내용 (Notice body text)
        created_by: 작성자 FK (Author user foreign key)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)
    """

    __tablename__ = "notices"

    # 공지 고유 식별자 — Notice unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 대상 매장 FK — NULL이면 조직 전체 공지 (NULL = org-wide, SET NULL on store delete)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)  # NULL = org-wide
    # 공지 제목 — Notice title
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 공지 내용 — Notice body (full text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 작성자 FK — User who created the notice (SET NULL: 사용자 삭제 시 null)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 소프트 삭제 일시 — Timestamp when notice was soft-deleted (NULL = active)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# AdditionalTask / AdditionalTaskAssignee / TaskEvidence (legacy) 제거됨.
# 신규 work item 시스템은 app/models/task.py 의 Task / TaskAssignee 사용.


class NoticeRead(Base):
    """공지사항 읽음 추적 모델 — 사용자별 공지 읽음 기록.

    Notice read tracking model — Records when a user reads an notice.
    Used to track read rates and identify unread users.

    Attributes:
        id: 고유 식별자 UUID
        notice_id: 공지사항 FK
        user_id: 읽은 사용자 FK
        read_at: 읽은 일시 UTC
    """

    __tablename__ = "notice_reads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    notice_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("notices.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("notice_id", "user_id", name="uq_notice_read"),
    )


class Voice(Base):
    """Voices 모델 — 아이디어/건의/이슈 통합 관리.

    Voice model — Unified idea/suggestion/issue tracking.
    Any role can create. Tracks with status workflow: open → in_progress → resolved.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK
        store_id: 관련 매장 FK (optional)
        title: 제목
        content: 본문 내용
        category: 유형 (idea, facility, equipment, safety, hr, other)
        status: 상태 (open, in_progress, resolved)
        priority: 우선순위 (low, normal, high, urgent)
        created_by: 작성자 FK
        resolved_by: 처리자 FK (optional)
        resolved_at: 처리 완료 일시 (optional)
        created_at: 생성 일시
        updated_at: 수정 일시
    """

    __tablename__ = "voices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(30), default="other")  # idea, facility, equipment, safety, hr, other
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, in_progress, resolved
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # low, normal, high, urgent
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 소프트 삭제 일시 — Timestamp when voice was soft-deleted (NULL = active)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
