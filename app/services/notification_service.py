"""알림 서비스 — 알림 비즈니스 로직.

Notification Service — Business logic for notification management.
Handles notification CRUD, read/unread operations, and auto-creation
for assignments, tasks, and announcements.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.models.checklist import ChecklistInstance, ChecklistInstanceItem
from app.models.communication import AdditionalTask, Announcement
from app.models.notification import Notification
from app.models.permission import Permission, RolePermission
from app.models.schedule import Schedule
from app.core.permissions import OWNER_PRIORITY
from app.models.user import Role, User
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
        try:
            result = await notification_repository.mark_read(db, notification_id, user_id)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

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
        try:
            count = await notification_repository.mark_all_read(db, user_id)
            await db.commit()
            return count
        except Exception:
            await db.rollback()
            raise

    # --- 자동 생성 (Auto-creation) ---

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
        message: str = f"New additional task: {task.title}"
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

    async def create_for_schedule_submit(
        self,
        db: AsyncSession,
        schedule: Schedule,
    ) -> list[Notification]:
        """스케줄 승인 요청 시 GM 이상 사용자에게 알림을 자동 생성합니다.

        Auto-create notifications for GM+ users when a schedule is submitted for approval.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule: 스케줄 객체 (Schedule object)

        Returns:
            list[Notification]: 생성된 알림 목록 (List of created notifications)
        """
        message: str = f"Schedule pending approval for {schedule.work_date}"

        # schedules:update 권한 보유 사용자 조회 — Find users with schedule approval permission
        gm_result = await db.execute(
            select(User.id)
            .join(Role, User.role_id == Role.id)
            .join(RolePermission, Role.id == RolePermission.role_id)
            .join(Permission, RolePermission.permission_id == Permission.id)
            .where(User.organization_id == schedule.organization_id)
            .where(User.is_active.is_(True))
            .where(Permission.code == "schedules:update")
        )
        gm_ids: list[UUID] = [row[0] for row in gm_result.all()]

        notifications: list[Notification] = []
        for uid in gm_ids:
            notification: Notification = await notification_repository.create_notification(
                db,
                organization_id=schedule.organization_id,
                user_id=uid,
                notification_type="schedule_pending",
                message=message,
                reference_type="schedule",
                reference_id=schedule.id,
            )
            notifications.append(notification)

        return notifications

    async def create_for_schedule_approve(
        self,
        db: AsyncSession,
        schedule: Schedule,
    ) -> Notification:
        """스케줄 승인 시 배정된 직원에게 알림을 자동 생성합니다.

        Auto-create a notification for the assigned staff when a schedule is approved.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule: 스케줄 객체 (Schedule object)

        Returns:
            Notification: 생성된 알림 (Created notification)
        """
        message: str = f"Your schedule for {schedule.work_date} has been approved"
        return await notification_repository.create_notification(
            db,
            organization_id=schedule.organization_id,
            user_id=schedule.user_id,
            notification_type="schedule_approved",
            message=message,
            reference_type="schedule",
            reference_id=schedule.id,
        )

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
        message: str = f"New announcement: {announcement.title}"
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

    async def create_for_checklist_submitted(
        self,
        db: AsyncSession,
        instance: "ChecklistInstance",
        staff_name: str,
        store_name: str,
    ) -> list["Notification"]:
        """체크리스트 완료 보고 시 해당 store의 SV/GM에게 알림을 생성합니다.

        Owner 제외, SV/GM만 대상.
        """
        from app.models.user import User
        from app.models.user_store import UserStore

        # checklists:update 권한 + is_manager + same store (Owner 제외)
        managers_q = (
            select(User)
            .join(UserStore, User.id == UserStore.user_id)
            .join(Role, User.role_id == Role.id)
            .join(RolePermission, Role.id == RolePermission.role_id)
            .join(Permission, RolePermission.permission_id == Permission.id)
            .where(
                UserStore.store_id == instance.store_id,
                UserStore.is_manager.is_(True),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                Permission.code == "checklists:update",
                Role.priority > OWNER_PRIORITY,  # Owner 제외 (비즈니스 규칙)
            )
        )
        result = await db.execute(managers_q)
        managers = result.scalars().all()

        message = f"Checklist completed: {store_name} — {staff_name}"
        notifications = []
        for manager in managers:
            notif = await notification_repository.create_notification(
                db,
                organization_id=instance.organization_id,
                user_id=manager.id,
                notification_type="checklist_submitted",
                message=message,
                reference_type="cl_instances",
                reference_id=instance.id,
            )
            notifications.append(notif)
        return notifications, managers

    async def create_for_checklist_re_review_item(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
        item: ChecklistInstanceItem,
    ) -> Notification:
        """체크리스트 재제출 시 reviewer에게 알림을 생성합니다.

        Auto-create a notification for the reviewer when staff resubmits.
        """
        message = "Checklist item resubmitted for re-review"
        return await notification_repository.create_notification(
            db,
            organization_id=instance.organization_id,
            user_id=item.reviewer_id,
            notification_type="checklist_re_review",
            message=message,
            reference_type="cl_instance_items",
            reference_id=item.id,
        )

    async def create_for_attendance_correction(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        corrected_by: UUID,
        field_name: str,
    ) -> list[Notification]:
        """근태 수정 시 GM 이상 사용자에게 알림을 자동 생성합니다.

        Auto-create notifications for GM+ users when an attendance record is corrected.
        """
        message: str = f"Attendance record corrected: {field_name}"

        # schedules:update 권한 보유 사용자 조회 — Find users with schedule management permission
        gm_result = await db.execute(
            select(User.id)
            .join(Role, User.role_id == Role.id)
            .join(RolePermission, Role.id == RolePermission.role_id)
            .join(Permission, RolePermission.permission_id == Permission.id)
            .where(User.organization_id == organization_id)
            .where(User.is_active.is_(True))
            .where(Permission.code == "schedules:update")
            .where(User.id != corrected_by)
        )
        gm_ids: list[UUID] = [row[0] for row in gm_result.all()]

        notifications: list[Notification] = []
        for uid in gm_ids:
            notification: Notification = await notification_repository.create_notification(
                db,
                organization_id=organization_id,
                user_id=uid,
                notification_type="attendance_corrected",
                message=message,
                reference_type="attendance",
                reference_id=attendance_id,
            )
            notifications.append(notification)
        return notifications

    async def create_for_substitute(
        self,
        db: AsyncSession,
        schedule: Schedule,
        old_user_id: UUID,
        new_user_id: UUID,
    ) -> list[Notification]:
        """대타 처리 시 기존 담당자와 새 담당자에게 알림을 자동 생성합니다.

        Auto-create notifications for old and new users on schedule substitution.
        """
        notifications: list[Notification] = []

        old_msg = f"Substituted out: schedule for {schedule.work_date} has been reassigned"
        notifications.append(await notification_repository.create_notification(
            db,
            organization_id=schedule.organization_id,
            user_id=old_user_id,
            notification_type="schedule_substitute",
            message=old_msg,
            reference_type="schedule",
            reference_id=schedule.id,
        ))

        new_msg = f"Substituted in: you have been assigned to schedule for {schedule.work_date}"
        notifications.append(await notification_repository.create_notification(
            db,
            organization_id=schedule.organization_id,
            user_id=new_user_id,
            notification_type="schedule_substitute",
            message=new_msg,
            reference_type="schedule",
            reference_id=schedule.id,
        ))

        return notifications


# 싱글턴 인스턴스 — Singleton instance
notification_service: NotificationService = NotificationService()
