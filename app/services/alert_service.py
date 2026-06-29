"""알림 서비스 — 알림 비즈니스 로직.

Alert Service — Business logic for alert management.
Handles alert CRUD, read/unread operations, and auto-creation
for assignments, tasks, and notices.

각 create_for_* 메서드는 수신자의 alert_preferences 를 확인하여
in-app 알림이 비활성화된 사용자는 자동 skip 한다. 이메일 발송 측에서는
should_send_email() 헬퍼로 동일하게 가드.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.core.alert_categories import (
    category_for_type,
    is_email_enabled,
    is_in_app_enabled,
)
from app.models.checklist import ChecklistInstance, ChecklistInstanceItem
from app.models.communication import Notice
from app.models.alert import Alert
from app.models.permission import Permission, RolePermission
from app.models.schedule import Schedule
from app.models.user import Role, User
from app.repositories.alert_repository import alert_repository


class AlertService:
    """알림 서비스.

    Alert service providing shared read/unread operations
    and auto-creation for various entity types.
    """

    # --- 공통 조회/읽음 처리 (Shared read/unread operations) ---

    async def list_alerts(
        self,
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Alert], int]:
        """사용자의 알림 목록을 페이지네이션하여 조회합니다.

        List paginated alerts for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Alert], int]: (알림 목록, 전체 개수)
                                                 (List of alerts, total count)
        """
        return await alert_repository.get_user_alerts(
            db, user_id, page, per_page
        )

    async def get_unread_count(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        """사용자의 읽지 않은 알림 수를 조회합니다.

        Get the count of unread alerts for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            int: 읽지 않은 알림 수 (Unread alert count)
        """
        return await alert_repository.get_unread_count(db, user_id)

    async def mark_read(
        self,
        db: AsyncSession,
        alert_id: UUID,
        user_id: UUID,
    ) -> bool:
        """단일 알림을 읽음 처리합니다.

        Mark a single alert as read.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            alert_id: 알림 UUID (Alert UUID)
            user_id: 사용자 UUID (User UUID)

        Returns:
            bool: 처리 성공 여부 (Whether the operation was successful)
        """
        try:
            result = await alert_repository.mark_read(db, alert_id, user_id)
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

        Mark all unread alerts as read for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            int: 읽음 처리된 알림 수 (Count of alerts marked as read)
        """
        try:
            count = await alert_repository.mark_all_read(db, user_id)
            await db.commit()
            return count
        except Exception:
            await db.rollback()
            raise

    # --- 사용자 알림 선호 가드 (Preference filtering) ---

    async def _filter_in_app_recipients(
        self,
        db: AsyncSession,
        user_ids: list[UUID],
        alert_type: str,
    ) -> list[UUID]:
        """user_ids 중 in-app 알림 활성화된 사용자만 반환. 카테고리 매핑 없으면 전부 통과.

        N+1 방지를 위해 한 쿼리로 prefs 조회.
        """
        if not user_ids:
            return []
        cat = category_for_type(alert_type)
        if cat is None:
            return user_ids
        result = await db.execute(
            select(User.id, User.alert_preferences).where(User.id.in_(user_ids))
        )
        return [uid for uid, prefs in result.all() if is_in_app_enabled(prefs, cat)]

    async def _is_in_app_enabled_for_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        alert_type: str,
    ) -> bool:
        """단일 사용자에 대한 in-app 알림 활성 여부."""
        cat = category_for_type(alert_type)
        if cat is None:
            return True
        result = await db.execute(
            select(User.alert_preferences).where(User.id == user_id)
        )
        prefs = result.scalar_one_or_none()
        return is_in_app_enabled(prefs, cat)

    async def should_send_email(
        self,
        db: AsyncSession,
        user_id: UUID,
        alert_type: str,
    ) -> bool:
        """이메일 발송 직전 가드 — 사용자 선호 체크. 외부 service 에서 호출."""
        cat = category_for_type(alert_type)
        if cat is None:
            return True
        result = await db.execute(
            select(User.alert_preferences).where(User.id == user_id)
        )
        prefs = result.scalar_one_or_none()
        return is_email_enabled(prefs, cat)

    # --- 자동 생성 (Auto-creation) ---

    async def create_for_schedule_submit(
        self,
        db: AsyncSession,
        schedule: Schedule,
    ) -> list[Alert]:
        """스케줄 승인 요청 시 GM 이상 사용자에게 알림을 자동 생성합니다.

        Auto-create alerts for GM+ users when a schedule is submitted for approval.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule: 스케줄 객체 (Schedule object)

        Returns:
            list[Alert]: 생성된 알림 목록 (List of created alerts)
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
        filtered = await self._filter_in_app_recipients(db, gm_ids, "schedule_pending")

        alerts: list[Alert] = []
        for uid in filtered:
            alert: Alert = await alert_repository.create_alert(
                db,
                organization_id=schedule.organization_id,
                user_id=uid,
                alert_type="schedule_pending",
                message=message,
                reference_type="schedule",
                reference_id=schedule.id,
            )
            alerts.append(alert)

        return alerts

    async def create_for_schedule_approve(
        self,
        db: AsyncSession,
        schedule: Schedule,
    ) -> Alert | None:
        """스케줄 승인 시 배정된 직원에게 알림을 자동 생성합니다.

        Auto-create a alert for the assigned staff when a schedule is approved.
        선호 비활성 시 None 반환.
        """
        if not await self._is_in_app_enabled_for_user(db, schedule.user_id, "schedule_approved"):
            return None
        message: str = f"Your schedule for {schedule.work_date} has been approved"
        return await alert_repository.create_alert(
            db,
            organization_id=schedule.organization_id,
            user_id=schedule.user_id,
            alert_type="schedule_approved",
            message=message,
            reference_type="schedule",
            reference_id=schedule.id,
        )

    async def create_for_schedule_assigned(
        self,
        db: AsyncSession,
        schedule: Schedule,
    ) -> Alert | None:
        """관리자가 직접 confirmed 스케줄을 만들 때 배정된 직원에게 알림을 생성합니다.

        Auto-create an alert for the assigned staff when an admin/GM creates a
        schedule directly in confirmed state (no separate approval step).
        선호 비활성 시 None 반환.
        """
        if not await self._is_in_app_enabled_for_user(db, schedule.user_id, "schedule_assigned"):
            return None
        message: str = f"New schedule assigned for {schedule.work_date}"
        return await alert_repository.create_alert(
            db,
            organization_id=schedule.organization_id,
            user_id=schedule.user_id,
            alert_type="schedule_assigned",
            message=message,
            reference_type="schedule",
            reference_id=schedule.id,
        )

    async def create_for_reply(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        recipient_id: UUID,
        author_name: str,
        context_label: str,
        reference_type: str,
        reference_id: UUID,
    ) -> Alert | None:
        """체크리스트/데일리리포트 등에 답변(메시지/코멘트)이 달렸을 때 알림 생성.
        선호 비활성 시 None 반환.
        """
        if not await self._is_in_app_enabled_for_user(db, recipient_id, "reply"):
            return None
        message = f"{author_name} replied on your {context_label}"
        return await alert_repository.create_alert(
            db,
            organization_id=organization_id,
            user_id=recipient_id,
            alert_type="reply",
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
        )

    async def create_for_report_submitted(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        recipient_id: UUID,
        author_name: str,
        context_label: str,
        reference_type: str,
        reference_id: UUID,
    ) -> Alert | None:
        """리포트가 제출되어 리뷰가 필요할 때 매장 리뷰어에게 알림. 선호 비활성 시 None."""
        if not await self._is_in_app_enabled_for_user(db, recipient_id, "report_submitted"):
            return None
        message = f"{author_name} submitted a {context_label}"
        return await alert_repository.create_alert(
            db,
            organization_id=organization_id,
            user_id=recipient_id,
            alert_type="report_submitted",
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
        )

    async def create_for_report_reviewed(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        recipient_id: UUID,
        reviewer_name: str,
        context_label: str,
        reference_type: str,
        reference_id: UUID,
    ) -> Alert | None:
        """리포트가 검토 완료되었을 때 작성자에게 알림. 선호 비활성 시 None."""
        if not await self._is_in_app_enabled_for_user(db, recipient_id, "report_reviewed"):
            return None
        message = f"{reviewer_name} reviewed your {context_label}"
        return await alert_repository.create_alert(
            db,
            organization_id=organization_id,
            user_id=recipient_id,
            alert_type="report_reviewed",
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
        )

    async def create_for_notice(
        self,
        db: AsyncSession,
        notice: Notice,
        user_ids: list[UUID],
    ) -> list[Alert]:
        """공지사항 생성 시 대상 사용자들에게 알림을 자동 생성합니다.

        Auto-create alerts for target users when an notice is created.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            notice: 공지사항 객체 (Notice object)
            user_ids: 대상 사용자 UUID 목록 (List of target user UUIDs)

        Returns:
            list[Alert]: 생성된 알림 목록 (List of created alerts)
        """
        message: str = f"New notice: {notice.title}"
        alerts: list[Alert] = []
        filtered = await self._filter_in_app_recipients(db, user_ids, "notice")

        for uid in filtered:
            alert: Alert = await alert_repository.create_alert(
                db,
                organization_id=notice.organization_id,
                user_id=uid,
                alert_type="notice",
                message=message,
                reference_type="notice",
                reference_id=notice.id,
            )
            alerts.append(alert)

        return alerts

    async def create_for_checklist_submitted(
        self,
        db: AsyncSession,
        instance: "ChecklistInstance",
        staff_name: str,
        store_name: str,
    ) -> list["Alert"]:
        """체크리스트 완료 보고 시 해당 store의 SV/GM에게 알림을 생성합니다.

        Owner 제외, SV/GM만 대상.
        """
        from app.models.user import User
        from app.models.user_store import UserStore

        # checklist_review:create 권한 + 해당 매장의 manager(is_manager=true) 인 사용자만.
        # Owner / Super Owner 는 자동 배정 시 is_manager=true → 자연 포함.
        # GM / SV 는 매장에 manager 로 명시 설정된 경우만 알림. (운영자가 매장별로 manager 지정)
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
                Permission.code == "checklist_review:create",
            )
            .distinct()
        )
        result = await db.execute(managers_q)
        managers = list(result.scalars().all())

        # in-app 활성 매니저만 알림 생성 — 이메일은 호출자가 별도 가드
        manager_ids = [m.id for m in managers]
        in_app_enabled_ids = set(
            await self._filter_in_app_recipients(db, manager_ids, "checklist_submitted")
        )

        message = f"Checklist completed: {store_name} — {staff_name}"
        alerts = []
        for manager in managers:
            if manager.id not in in_app_enabled_ids:
                continue
            notif = await alert_repository.create_alert(
                db,
                organization_id=instance.organization_id,
                user_id=manager.id,
                alert_type="checklist_submitted",
                message=message,
                reference_type="cl_instances",
                reference_id=instance.id,
            )
            alerts.append(notif)
        # 이메일 발송은 전체 매니저 대상으로 호출자가 should_send_email 가드 적용
        return alerts, managers

    async def create_for_checklist_re_review_item(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
        item: ChecklistInstanceItem,
    ) -> Alert | None:
        """체크리스트 재제출 시 reviewer에게 알림을 생성합니다. 선호 비활성 시 None."""
        if not await self._is_in_app_enabled_for_user(db, item.reviewer_id, "checklist_re_review"):
            return None
        message = "Checklist item resubmitted for re-review"
        return await alert_repository.create_alert(
            db,
            organization_id=instance.organization_id,
            user_id=item.reviewer_id,
            alert_type="checklist_re_review",
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
    ) -> list[Alert]:
        """근태 수정 시 GM 이상 사용자에게 알림을 자동 생성합니다.

        Auto-create alerts for GM+ users when an attendance record is corrected.
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
        filtered = await self._filter_in_app_recipients(db, gm_ids, "attendance_corrected")

        alerts: list[Alert] = []
        for uid in filtered:
            alert: Alert = await alert_repository.create_alert(
                db,
                organization_id=organization_id,
                user_id=uid,
                alert_type="attendance_corrected",
                message=message,
                reference_type="attendance",
                reference_id=attendance_id,
            )
            alerts.append(alert)
        return alerts

    async def create_for_warning(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        subject_user_id: UUID,
        warning_id: UUID,
        title: str,
        alert_type: str = "warning",
    ) -> Alert | None:
        """경고 관련 in-app 알림 생성. 선호 비활성('warning' 카테고리) 시 None.

        alert_type:
            'warning'        — 발행 ("You have received a warning").
            'warning_resign' — 방식 전환(wet→digital)으로 앱 재서명 필요.
        둘 다 'warning' 카테고리 토글을 따른다(category_for_type).
        """
        if not await self._is_in_app_enabled_for_user(db, subject_user_id, "warning"):
            return None
        if alert_type == "warning_resign":
            message = f"Please re-sign your warning in the app: {title}"
        else:
            message = f"You have received a warning: {title}"
        return await alert_repository.create_alert(
            db,
            organization_id=organization_id,
            user_id=subject_user_id,
            alert_type=alert_type,
            message=message,
            reference_type="warning",
            reference_id=warning_id,
        )

    async def create_for_substitute(
        self,
        db: AsyncSession,
        schedule: Schedule,
        old_user_id: UUID,
        new_user_id: UUID,
    ) -> list[Alert]:
        """대타 처리 시 기존 담당자와 새 담당자에게 알림을 자동 생성합니다. 선호 비활성자는 skip."""
        alerts: list[Alert] = []

        if await self._is_in_app_enabled_for_user(db, old_user_id, "schedule_substitute"):
            old_msg = f"Substituted out: schedule for {schedule.work_date} has been reassigned"
            alerts.append(await alert_repository.create_alert(
                db,
                organization_id=schedule.organization_id,
                user_id=old_user_id,
                alert_type="schedule_substitute",
                message=old_msg,
                reference_type="schedule",
                reference_id=schedule.id,
            ))

        if await self._is_in_app_enabled_for_user(db, new_user_id, "schedule_substitute"):
            new_msg = f"Substituted in: you have been assigned to schedule for {schedule.work_date}"
            alerts.append(await alert_repository.create_alert(
                db,
                organization_id=schedule.organization_id,
                user_id=new_user_id,
                alert_type="schedule_substitute",
                message=new_msg,
                reference_type="schedule",
                reference_id=schedule.id,
            ))

        return alerts


# 싱글턴 인스턴스 — Singleton instance
alert_service: AlertService = AlertService()
