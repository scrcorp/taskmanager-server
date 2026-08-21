"""알림 서비스 — 알림 비즈니스 로직.

Alert Service — Business logic for alert management.
Handles alert CRUD, read/unread operations, and auto-creation
for assignments, tasks, and notices.

각 create_for_* 메서드는 수신자의 alert_preferences 를 확인하여
in-app 알림이 비활성화된 사용자는 자동 skip 한다. 이메일 발송 측에서는
should_send_email() 헬퍼로 동일하게 가드.
"""

from datetime import date, datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import or_, select

from app.services.push_dispatch import dispatch_alert_push
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

    async def _create_alert(self, db: AsyncSession, **kwargs) -> Alert:
        """알림 행을 만들고 웹 푸시 발송을 예약한다.

        alert_repository.create_alert 를 직접 부르지 말고 항상 이걸 쓴다 —
        그래야 "알림함에 쌓이면 푸시도 나간다" 는 규칙이 한 곳에서 지켜진다.
        푸시는 백그라운드로 나가며, 커밋되지 않은 알림에는 발송되지 않는다.
        """
        alert: Alert = await alert_repository.create_alert(db, **kwargs)
        dispatch_alert_push(alert)
        return alert

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
            alert: Alert = await self._create_alert(
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
        return await self._create_alert(
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
        return await self._create_alert(
            db,
            organization_id=schedule.organization_id,
            user_id=schedule.user_id,
            alert_type="schedule_assigned",
            message=message,
            reference_type="schedule",
            reference_id=schedule.id,
        )

    async def create_for_fixed_schedule_changed(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        user_id: UUID,
        group_id: UUID,
        message: str,
    ) -> Alert | None:
        """고정 근무(패턴 그룹) 생성/수정/이동/삭제 → 대상 직원에게 **작업 1회 = 알림 1건**(D-e).

        건별(실체화된 날짜마다) 알림은 내지 않는다 — create_entry 가 pattern_stamp 있으면
        schedule_assigned 를 건너뛰는 것과 짝. message 는 호출자가 그룹 요약(요일·시간·기간)으로 만든다.
        reference = pattern_group(group_id). 선호 비활성 시 None. 커밋은 호출자가 한다.
        """
        if not await self._is_in_app_enabled_for_user(db, user_id, "fixed_schedule_changed"):
            return None
        return await self._create_alert(
            db,
            organization_id=organization_id,
            user_id=user_id,
            alert_type="fixed_schedule_changed",
            message=message[:1000],
            reference_type="pattern_group",
            reference_id=group_id,
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
        return await self._create_alert(
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
        return await self._create_alert(
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
        return await self._create_alert(
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
            alert: Alert = await self._create_alert(
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
            notif = await self._create_alert(
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
        return await self._create_alert(
            db,
            organization_id=instance.organization_id,
            user_id=item.reviewer_id,
            alert_type="checklist_re_review",
            message=message,
            reference_type="cl_instance_items",
            reference_id=item.id,
        )

    def attendance_recipient_query(
        self,
        *,
        organization_id: UUID,
        store_id: UUID,
        exclude_user_id: UUID | None = None,
    ):
        """근태 알림(근태수정 / 조기출근 강행 / 겹침출근) 수신자.

        Recipient rule for attendance alerts:
        **that store's checked managers, plus Owner (and Super Owner).**

        포함 규칙 — 아래 둘 중 **하나만 맞으면** 수신자다:
          1. 그 매장에 `is_manager=True` 로 배정된 사람 — 콘솔 Staff > Detail 의
             매장별 manager 체크가 그대로 기준이다. **GM 도 여기에 걸려야 받는다**
             (그 매장 GM 만 받는다는 뜻). SV 든 GM 든 직급으로는 안 들어온다.
          2. `priority <= OWNER_PRIORITY` (Owner / Super Owner) — 오너는 오너라서
             받는다. 매장 배정과 무관하게 항상.

        그 밖에:
          - active + 미삭제. 비활성/퇴사자는 조치할 수 없는 사람이다.
          - 본인 제외 — 자기 행위를 자기에게 알리지 않는다.

        `schedules:update` **권한**으로 고르지 않는 이유: 그 권한은 SV 기본 세트에
        들어 있는 데다 매장 스코프가 없어서, 조직 전체 SV/GM 이 전 매장 근태 알림을
        받아버린다(실제 발생한 문제). 직급이 아니라 **매장 manager 체크**가 기준이다.

        매장 조건을 join 이 아니라 EXISTS 로 거는 이유: join 이면 `user_stores` 행이
        하나도 없는 Owner 가 통째로 빠진다(규칙 2 가 무력화된다). EXISTS 는 행을
        늘리지도 않아 중복 발송 걱정도 없다.

        in-app 과 email 이 이 쿼리를 공유한다. 수신 범위를 바꿀 일이 생기면
        **여기만** 고칠 것 — 따로 짜면 두 채널이 조용히 갈린다.
        """
        from app.core.permissions import OWNER_PRIORITY
        from app.models.user_store import UserStore

        manages_this_store = (
            select(UserStore.id)
            .where(
                UserStore.user_id == User.id,
                UserStore.store_id == store_id,
                UserStore.is_manager.is_(True),
            )
            .exists()
        )
        query = (
            select(User.id, User.email)
            .join(Role, User.role_id == Role.id)
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                or_(manages_this_store, Role.priority <= OWNER_PRIORITY),
            )
        )
        if exclude_user_id is not None:
            query = query.where(User.id != exclude_user_id)
        return query

    async def _attendance_recipient_ids(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        store_id: UUID,
        exclude_user_id: UUID | None,
        alert_type: str,
    ) -> list[UUID]:
        """근태 알림 in-app 수신자 id 목록 (선호도 가드 적용 후)."""
        result = await db.execute(
            self.attendance_recipient_query(
                organization_id=organization_id,
                store_id=store_id,
                exclude_user_id=exclude_user_id,
            )
        )
        recipient_ids: list[UUID] = [row[0] for row in result.all()]
        return await self._filter_in_app_recipients(db, recipient_ids, alert_type)

    async def create_for_attendance_correction(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        store_id: UUID,
        corrected_by: UUID,
        field_name: str,
    ) -> list[Alert]:
        """근태 수정 시 그 매장 manager + Owner 에게 알림을 자동 생성합니다.

        Auto-create alerts for that store's checked managers and Owners when an
        attendance record is corrected. 수신자 규칙은 `attendance_recipient_query` 참조.
        """
        message: str = f"Attendance record corrected: {field_name}"

        filtered = await self._attendance_recipient_ids(
            db,
            organization_id=organization_id,
            store_id=store_id,
            exclude_user_id=corrected_by,
            alert_type="attendance_corrected",
        )

        alerts: list[Alert] = []
        for uid in filtered:
            alert: Alert = await self._create_alert(
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

    async def create_for_early_clock_in(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        store_id: UUID,
        staff_user_id: UUID,
        staff_name: str,
        minutes_early: int,
        scheduled_start_label: str | None = None,
    ) -> list[Alert]:
        """조기 출근 강행 시 그 매장 manager + Owner 에게 알림을 생성합니다.

        Auto-create alerts when staff force an early clock-in with a reason.
        매니저가 현장에 없어서 벌어지는 일이라, 늦게라도 반드시 눈에 들어와야 한다.
        수신자 규칙은 `attendance_recipient_query` 참조.
        email 은 호출자가 should_send_email 가드와 함께 별도로 보낸다
        (checklist/report 알림과 동일한 분업).
        """
        # 문구에 **예정 시각을 날짜와 함께** 싣는다("Aug 19, 5:00 PM"). 분 수만 있으면
        # 날짜가 하루 어긋난 스케줄(2026-08 오염 사고)이 "1439분 일찍" 이라는 숫자로만
        # 보여서, 받는 사람이 이상한 건 알아도 무엇이 이상한지 알 수 없다.
        message = (
            f"{staff_name} clocked in {minutes_early} minutes before their shift"
        )
        if scheduled_start_label:
            message += f" (scheduled {scheduled_start_label})"

        filtered = await self._attendance_recipient_ids(
            db,
            organization_id=organization_id,
            store_id=store_id,
            exclude_user_id=staff_user_id,
            alert_type="early_clock_in_override",
        )

        alerts: list[Alert] = []
        for uid in filtered:
            alerts.append(
                await self._create_alert(
                    db,
                    organization_id=organization_id,
                    user_id=uid,
                    alert_type="early_clock_in_override",
                    message=message,
                    reference_type="attendance",
                    reference_id=attendance_id,
                )
            )
        return alerts

    # 겹침 알림 중복 억제 창(분). 직원이 "이거 아님" 을 반복하면 매니저 알림함이
    # 도배되므로 같은 사람·같은 영업일에 대해 이 간격 안에는 1건만 보낸다.
    OVERLAP_ALERT_DEDUP_MINUTES = 60

    async def create_for_overlapping_clock_in(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        store_id: UUID,
        staff_user_id: UUID,
        staff_name: str,
        work_date: date,
    ) -> list[Alert]:
        """겹쳐 출근(D15) 시 그 매장 manager + Owner 알림. 같은 (직원, 영업일) 은 60분에 1건.

        직원은 이 상태를 스스로 정리할 수 없다 — 한쪽을 취소·정정하는 건 매니저
        권한이다. 그래서 알림이 유일한 즉시 전달 경로다.
        """
        from datetime import timedelta as _td

        from app.models.attendance import Attendance as _Attendance

        # 같은 직원의 같은 영업일 attendance 들에 대해 최근에 나간 알림이 있으면 skip.
        sibling_ids = list(
            (
                await db.execute(
                    select(_Attendance.id).where(
                        _Attendance.user_id == staff_user_id,
                        _Attendance.work_date == work_date,
                    )
                )
            )
            .scalars()
            .all()
        )
        if sibling_ids:
            since = datetime.now(timezone.utc) - _td(
                minutes=self.OVERLAP_ALERT_DEDUP_MINUTES
            )
            recent = await db.scalar(
                select(Alert.id)
                .where(
                    # 컬럼명은 `type` 이다 (repository 인자명만 alert_type).
                    Alert.type == "overlapping_clock_in",
                    Alert.reference_type == "attendance",
                    Alert.reference_id.in_(sibling_ids),
                    Alert.created_at >= since,
                )
                .limit(1)
            )
            if recent is not None:
                return []

        message = (
            f"{staff_name} clocked in to a second shift while the first is still open"
        )
        filtered = await self._attendance_recipient_ids(
            db,
            organization_id=organization_id,
            store_id=store_id,
            exclude_user_id=staff_user_id,
            alert_type="overlapping_clock_in",
        )

        alerts: list[Alert] = []
        for uid in filtered:
            alerts.append(
                await alert_repository.create_alert(
                    db,
                    organization_id=organization_id,
                    user_id=uid,
                    alert_type="overlapping_clock_in",
                    message=message,
                    reference_type="attendance",
                    reference_id=attendance_id,
                )
            )
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
        return await self._create_alert(
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
            alerts.append(await self._create_alert(
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
            alerts.append(await self._create_alert(
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
