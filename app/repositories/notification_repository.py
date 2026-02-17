"""알림 레포지토리 — 알림 관련 DB 쿼리 담당.

Notification Repository — Handles all notification-related database queries.
Extends BaseRepository with user-specific notification operations.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    """알림 레포지토리.

    Notification repository with user-specific read/unread operations.

    Extends:
        BaseRepository[Notification]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the notification repository with Notification model.
        """
        super().__init__(Notification)

    async def get_user_notifications(
        self,
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Notification], int]:
        """사용자의 알림 목록을 페이지네이션하여 조회합니다.

        Retrieve paginated notifications for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Notification], int]: (알림 목록, 전체 개수)
                                                 (List of notifications, total count)
        """
        query: Select = (
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)

    async def get_unread_count(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        """사용자의 읽지 않은 알림 수를 조회합니다.

        Get the count of unread notifications for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            int: 읽지 않은 알림 수 (Count of unread notifications)
        """
        query: Select = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
        )
        count: int = (await db.execute(query)).scalar() or 0
        return count

    async def mark_read(
        self,
        db: AsyncSession,
        notification_id: UUID,
        user_id: UUID,
    ) -> bool:
        """단일 알림을 읽음 처리합니다.

        Mark a single notification as read.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            notification_id: 알림 UUID (Notification UUID)
            user_id: 사용자 UUID (User UUID)

        Returns:
            bool: 처리 성공 여부 (Whether the operation was successful)
        """
        result = await db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
            .values(is_read=True)
        )
        await db.flush()
        return result.rowcount > 0

    async def mark_all_read(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        """사용자의 모든 읽지 않은 알림을 읽음 처리합니다.

        Mark all unread notifications as read for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            int: 업데이트된 알림 수 (Count of updated notifications)
        """
        result = await db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True)
        )
        await db.flush()
        return result.rowcount

    async def create_notification(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        notification_type: str,
        message: str,
        reference_type: str | None = None,
        reference_id: UUID | None = None,
    ) -> Notification:
        """새 알림을 생성합니다.

        Create a new notification.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            user_id: 수신자 UUID (Recipient user UUID)
            notification_type: 알림 유형 (Notification type)
            message: 알림 메시지 (Notification message)
            reference_type: 참조 유형, 선택 (Optional reference type)
            reference_id: 참조 ID, 선택 (Optional reference UUID)

        Returns:
            Notification: 생성된 알림 (Created notification)
        """
        notification: Notification = Notification(
            organization_id=organization_id,
            user_id=user_id,
            type=notification_type,
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
        )
        db.add(notification)
        await db.flush()
        await db.refresh(notification)
        return notification


# 싱글턴 인스턴스 — Singleton instance
notification_repository: NotificationRepository = NotificationRepository()
