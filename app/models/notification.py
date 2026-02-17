"""알림 관련 SQLAlchemy ORM 모델 정의.

Notification SQLAlchemy ORM model definitions.
Implements a polymorphic notification system where each notification
can reference different entity types via reference_type and reference_id.

Tables:
    - notifications: 사용자 알림 (User notifications with polymorphic references)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Notification(Base):
    """알림 모델 — 사용자에게 전달되는 시스템 알림.

    Notification model — System notifications delivered to users.
    Uses a polymorphic reference pattern (reference_type + reference_id)
    to link back to the source entity that triggered the notification.

    Notification Types (type 필드 값):
        - "work_assigned": 근무 배정 알림 (New work assignment notification)
        - "additional_task": 추가 업무 알림 (New additional task notification)
        - "announcement": 공지사항 알림 (New announcement notification)
        - "task_completed": 업무 완료 알림 (Task completion notification)

    Reference Types (reference_type 필드 값):
        - "work_assignment": WorkAssignment 참조 (Links to work_assignments table)
        - "additional_task": AdditionalTask 참조 (Links to additional_tasks table)
        - "announcement": Announcement 참조 (Links to announcements table)

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope)
        user_id: 수신자 FK (Recipient user foreign key)
        type: 알림 유형 (Notification type, see above)
        message: 알림 메시지 (Human-readable notification message)
        reference_type: 참조 엔티티 유형 (Referenced entity table name)
        reference_id: 참조 엔티티 ID (Referenced entity UUID)
        is_read: 읽음 여부 (Whether the user has read this notification)
        created_at: 생성 일시 UTC (Creation timestamp)
    """

    __tablename__ = "notifications"

    # 알림 고유 식별자 — Notification unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 수신자 FK — Target user who receives this notification
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # 알림 유형 — Notification type (work_assigned | additional_task | announcement | task_completed)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    # 알림 메시지 — Human-readable message displayed to the user
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    # 참조 엔티티 유형 — Polymorphic reference: entity table name (work_assignment | additional_task | announcement)
    reference_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 참조 엔티티 ID — Polymorphic reference: UUID of the source entity
    reference_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # 읽음 여부 — False=미읽음, True=읽음 (Unread by default)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    # 생성 일시 — Notification creation timestamp (UTC, immutable)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
