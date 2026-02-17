"""알림 서비스 — 알림 비즈니스 로직.

Notification Service — Business logic for notification management.
Handles notification CRUD, read/unread operations, and auto-creation
for assignments, tasks, and announcements.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment import WorkAssignment
from app.models.communication import AdditionalTask, Announcement
from app.models.notification import Notification
from app.repositories.notification_repository import notification_repository


class NotificationService:
    """알림 서비스.

    Notification service providing shared read/unread operations
    and auto-creation for various entity types.
    """

    # --- 공통 조회/읽음 처리 (Shared read/unread operations) ---

    async def list_notifications(
        self,
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Notification], int]:
        """사용자의 알림 목록을 페이지네이션하여 조회합니다.

        List paginated notifications for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Notification], int]: (알림 목록, 전체 개수)
                                                 (List of notifications, total count)
        """
        return await notification_repository.get_user_notifications(
            db, user_id, page, per_page
        )

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
            int: 읽지 않은 알림 수 (Unread notification count)
        """
        return await notification_repository.get_unread_count(db, user_id)

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
        return await notification_repository.mark_read(db, notification_id, user_id)

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
            int: 읽음 처리된 알림 수 (Count of notifications marked as read)
        """
        return await notification_repository.mark_all_read(db, user_id)

    # --- 자동 생성 (Auto-creation) ---

    async def create_for_assignment(
        self,
        db: AsyncSession,
        assignment: WorkAssignment,
    ) -> Notification:
        """업무 배정 시 알림을 자동 생성합니다.

        Auto-create a notification when a work assignment is created.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment: 업무 배정 객체 (Work assignment object)

        Returns:
            Notification: 생성된 알림 (Created notification)
        """
        message: str = f"새 업무가 배정되었습니다 (New work assignment for {assignment.work_date})"
        return await notification_repository.create_notification(
            db,
            organization_id=assignment.organization_id,
            user_id=assignment.user_id,
            notification_type="work_assigned",
            message=message,
            reference_type="work_assignment",
            reference_id=assignment.id,
        )

    async def create_for_task(
        self,
        db: AsyncSession,
        task: AdditionalTask,
        assignee_ids: list[UUID],
    ) -> list[Notification]:
        """추가 업무 생성 시 담당자들에게 알림을 자동 생성합니다.

        Auto-create notifications for assignees when an additional task is created.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task: 추가 업무 객체 (Additional task object)
            assignee_ids: 담당자 UUID 목록 (List of assignee UUIDs)

        Returns:
            list[Notification]: 생성된 알림 목록 (List of created notifications)
        """
        message: str = f"새 추가 업무가 배정되었습니다: {task.title} (New additional task: {task.title})"
        notifications: list[Notification] = []

        for uid in assignee_ids:
            notification: Notification = await notification_repository.create_notification(
                db,
                organization_id=task.organization_id,
                user_id=uid,
                notification_type="additional_task",
                message=message,
                reference_type="additional_task",
                reference_id=task.id,
            )
            notifications.append(notification)

        return notifications

    async def create_for_announcement(
        self,
        db: AsyncSession,
        announcement: Announcement,
        user_ids: list[UUID],
    ) -> list[Notification]:
        """공지사항 생성 시 대상 사용자들에게 알림을 자동 생성합니다.

        Auto-create notifications for target users when an announcement is created.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            announcement: 공지사항 객체 (Announcement object)
            user_ids: 대상 사용자 UUID 목록 (List of target user UUIDs)

        Returns:
            list[Notification]: 생성된 알림 목록 (List of created notifications)
        """
        message: str = f"새 공지사항: {announcement.title} (New announcement: {announcement.title})"
        notifications: list[Notification] = []

        for uid in user_ids:
            notification: Notification = await notification_repository.create_notification(
                db,
                organization_id=announcement.organization_id,
                user_id=uid,
                notification_type="announcement",
                message=message,
                reference_type="announcement",
                reference_id=announcement.id,
            )
            notifications.append(notification)

        return notifications


# 싱글턴 인스턴스 — Singleton instance
notification_service: NotificationService = NotificationService()
