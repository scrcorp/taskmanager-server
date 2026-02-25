"""스케줄 서비스 — 스케줄 비즈니스 로직.

Schedule Service — Business logic for schedule management.
Handles schedule creation, updates, status transitions (draft → pending → approved),
automatic work_assignment creation upon approval, and response building.
"""

from datetime import date, datetime, time, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import ShiftPreset, Store
from app.models.schedule import Schedule
from app.models.user import User
from app.models.work import Position, Shift
from app.repositories.schedule_repository import schedule_repository
from app.schemas.common import ScheduleCreate, ScheduleUpdate
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError


class ScheduleService:
    """스케줄 서비스.

    Schedule service handling creation, status transitions,
    approval with automatic work_assignment creation, and response building.
    """

    async def _validate_store_ownership(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> Store:
        """매장이 해당 조직에 속하는지 검증합니다.

        Verify that a store belongs to the specified organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Store: 검증된 매장 (Verified store)

        Raises:
            NotFoundError: 매장이 없을 때 (When store not found)
            ForbiddenError: 다른 조직 매장일 때 (When store belongs to another org)
        """
        result = await db.execute(select(Store).where(Store.id == store_id))
        store: Store | None = result.scalar_one_or_none()

        if store is None:
            raise NotFoundError("매장을 찾을 수 없습니다 (Store not found)")
        if store.organization_id != organization_id:
            raise ForbiddenError("해당 매장에 대한 권한이 없습니다 (No permission for this store)")
        return store

    @staticmethod
    def _parse_time(time_str: str | None) -> time | None:
        """시간 문자열을 time 객체로 변환합니다.

        Parse a time string ("HH:MM") to a time object.

        Args:
            time_str: 시간 문자열 "HH:MM" 또는 None (Time string or None)

        Returns:
            time | None: 파싱된 time 객체 또는 None (Parsed time or None)
        """
        if time_str is None:
            return None
        parts: list[str] = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))

    @staticmethod
    def _format_time(t: time | None) -> str | None:
        """time 객체를 문자열로 변환합니다.

        Format a time object to "HH:MM" string.

        Args:
            t: time 객체 또는 None (Time object or None)

        Returns:
            str | None: "HH:MM" 형식 문자열 또는 None (Formatted string or None)
        """
        if t is None:
            return None
        return t.strftime("%H:%M")

    async def create_schedule(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: ScheduleCreate,
        created_by: UUID,
    ) -> Schedule:
        """새 스케줄 초안을 생성합니다.

        Create a new draft schedule with duplicate check.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            data: 스케줄 생성 데이터 (Schedule creation data)
            created_by: 작성자 UUID (Creator's UUID — usually Supervisor)

        Returns:
            Schedule: 생성된 스케줄 (Created schedule)

        Raises:
            NotFoundError: 매장이 없을 때 (When store not found)
            ForbiddenError: 다른 조직 매장일 때 (When store belongs to another org)
            DuplicateError: 같은 조합의 스케줄이 이미 존재할 때 (When duplicate schedule exists)
        """
        store_id: UUID = UUID(data.store_id)
        user_id: UUID = UUID(data.user_id)
        shift_id: UUID | None = UUID(data.shift_id) if data.shift_id else None
        position_id: UUID | None = UUID(data.position_id) if data.position_id else None
        preset_id: UUID | None = UUID(data.preset_id) if data.preset_id else None

        # 매장 소유권 검증 — Verify store ownership
        await self._validate_store_ownership(db, store_id, organization_id)

        # 프리셋 연동 — If preset_id provided, auto-fill shift_id and start/end times
        start_time: time | None = self._parse_time(data.start_time)
        end_time: time | None = self._parse_time(data.end_time)

        if preset_id is not None:
            preset_result = await db.execute(
                select(ShiftPreset).where(ShiftPreset.id == preset_id)
            )
            preset: ShiftPreset | None = preset_result.scalar_one_or_none()
            if preset is None:
                raise NotFoundError("Shift preset not found")
            # Auto-fill shift_id from preset if not explicitly provided
            if shift_id is None:
                shift_id = preset.shift_id
            # Auto-fill start/end times from preset if not explicitly provided
            if start_time is None:
                start_time = preset.start_time
            if end_time is None:
                end_time = preset.end_time

        # 중복 스케줄 검사 — Check for duplicate schedule
        is_duplicate: bool = await schedule_repository.check_duplicate(
            db, user_id, store_id, data.work_date, shift_id
        )
        if is_duplicate:
            raise DuplicateError(
                "해당 날짜에 동일한 스케줄이 이미 존재합니다 "
                "(A schedule for this combination on this date already exists)"
            )

        schedule: Schedule = await schedule_repository.create(
            db,
            {
                "organization_id": organization_id,
                "store_id": store_id,
                "user_id": user_id,
                "shift_id": shift_id,
                "position_id": position_id,
                "work_date": data.work_date,
                "start_time": start_time,
                "end_time": end_time,
                "status": "draft",
                "note": data.note,
                "created_by": created_by,
            },
        )

        return schedule

    async def update_schedule(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
        data: ScheduleUpdate,
        user_id: UUID,
    ) -> Schedule:
        """스케줄을 수정합니다 (draft 또는 pending 상태에서만 가능).

        Update a schedule (only allowed in draft or pending status).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule_id: 스케줄 UUID (Schedule UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 스케줄 수정 데이터 (Schedule update data)
            user_id: 수정자 UUID (Editor's UUID)

        Returns:
            Schedule: 수정된 스케줄 (Updated schedule)

        Raises:
            NotFoundError: 스케줄이 없을 때 (When schedule not found)
            BadRequestError: 수정 불가 상태일 때 (When schedule cannot be edited)
        """
        schedule: Schedule | None = await schedule_repository.get_by_id_with_org(
            db, schedule_id, organization_id
        )
        if schedule is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")

        if schedule.status not in ("draft", "pending"):
            raise BadRequestError(
                "승인 완료 또는 취소된 스케줄은 수정할 수 없습니다 "
                "(Cannot edit an approved or cancelled schedule)"
            )

        # 수정 데이터 구성 — Build update dict from provided fields
        update_data: dict = {}
        if data.shift_id is not None:
            update_data["shift_id"] = UUID(data.shift_id)
        if data.position_id is not None:
            update_data["position_id"] = UUID(data.position_id)
        if data.start_time is not None:
            update_data["start_time"] = self._parse_time(data.start_time)
        if data.end_time is not None:
            update_data["end_time"] = self._parse_time(data.end_time)
        if data.note is not None:
            update_data["note"] = data.note

        if not update_data:
            return schedule

        updated: Schedule | None = await schedule_repository.update(
            db, schedule_id, update_data, organization_id
        )
        if updated is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")

        return updated

    async def submit_for_approval(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
    ) -> Schedule:
        """스케줄을 승인 요청 상태로 변경합니다 (draft → pending).

        Submit a schedule for approval (change status from draft to pending).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule_id: 스케줄 UUID (Schedule UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Schedule: 상태 변경된 스케줄 (Schedule with updated status)

        Raises:
            NotFoundError: 스케줄이 없을 때 (When schedule not found)
            BadRequestError: draft 상태가 아닐 때 (When not in draft status)
        """
        schedule: Schedule | None = await schedule_repository.get_by_id_with_org(
            db, schedule_id, organization_id
        )
        if schedule is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")

        if schedule.status != "draft":
            raise BadRequestError(
                "초안 상태의 스케줄만 승인 요청할 수 있습니다 "
                "(Only draft schedules can be submitted for approval)"
            )

        schedule.status = "pending"
        await db.flush()
        await db.refresh(schedule)

        # GM+ 사용자에게 승인 요청 알림 — Notify GM+ users about pending approval
        from app.services.notification_service import notification_service
        await notification_service.create_for_schedule_submit(db, schedule)

        return schedule

    async def approve_schedule(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
        approved_by: UUID,
    ) -> Schedule:
        """스케줄을 승인하고 work_assignment를 자동 생성합니다.

        Approve a schedule and automatically create a work_assignment.
        Uses the existing assignment_service.create_assignment() logic
        for checklist snapshot generation.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule_id: 스케줄 UUID (Schedule UUID)
            organization_id: 조직 UUID (Organization UUID)
            approved_by: 승인자 UUID (Approver's UUID — usually GM)

        Returns:
            Schedule: 승인된 스케줄 (Approved schedule with work_assignment_id)

        Raises:
            NotFoundError: 스케줄이 없을 때 (When schedule not found)
            BadRequestError: pending 상태가 아닐 때 또는 shift/position이 없을 때
                             (When not in pending status or missing shift/position)
        """
        schedule: Schedule | None = await schedule_repository.get_by_id_with_org(
            db, schedule_id, organization_id
        )
        if schedule is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")

        if schedule.status != "pending":
            raise BadRequestError(
                "승인 대기 상태의 스케줄만 승인할 수 있습니다 "
                "(Only pending schedules can be approved)"
            )

        # work_assignment 생성에 shift_id, position_id 필요 — Validate required fields
        if schedule.shift_id is None or schedule.position_id is None:
            raise BadRequestError(
                "Shift와 Position이 설정된 스케줄만 승인할 수 있습니다. "
                "스케줄을 수정하여 Shift와 Position을 지정해주세요. "
                "(Schedule must have shift and position set before approval. "
                "Please edit the schedule to set shift and position.)"
            )

        # 기존 assignment_service를 사용하여 work_assignment 생성
        # Use existing assignment_service to create work_assignment with checklist snapshot
        from app.schemas.common import AssignmentCreate
        from app.services.assignment_service import assignment_service

        assignment_data: AssignmentCreate = AssignmentCreate(
            store_id=str(schedule.store_id),
            shift_id=str(schedule.shift_id),
            position_id=str(schedule.position_id),
            user_id=str(schedule.user_id),
            work_date=schedule.work_date,
        )

        try:
            assignment = await assignment_service.create_assignment(
                db,
                organization_id=organization_id,
                data=assignment_data,
                assigned_by=approved_by,
            )
        except Exception as e:
            # assignment 생성 실패 시 스케줄 상태를 변경하지 않음
            # Don't change schedule status if assignment creation fails
            raise BadRequestError(
                f"배정 생성 실패: {e.detail if hasattr(e, 'detail') else str(e)} "
                f"(Work assignment creation failed)"
            )

        # 스케줄 상태 업데이트 — Update schedule status
        schedule.status = "approved"
        schedule.approved_by = approved_by
        schedule.approved_at = datetime.now(timezone.utc)
        schedule.work_assignment_id = assignment.id

        # 승인 이력 생성 — Create approval audit record
        await schedule_repository.create_approval(
            db,
            {
                "schedule_id": schedule.id,
                "action": "approve",
                "user_id": approved_by,
            },
        )

        await db.flush()
        await db.refresh(schedule)

        # 배정된 직원에게 승인 알림 — Notify assigned staff about approval
        from app.services.notification_service import notification_service
        await notification_service.create_for_schedule_approve(db, schedule)

        return schedule

    async def cancel_schedule(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
        user_id: UUID,
    ) -> Schedule:
        """스케줄을 취소합니다 (draft 또는 pending 상태에서만 가능).

        Cancel a schedule (only allowed in draft or pending status).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule_id: 스케줄 UUID (Schedule UUID)
            organization_id: 조직 UUID (Organization UUID)
            user_id: 취소 요청자 UUID (User who cancels)

        Returns:
            Schedule: 취소된 스케줄 (Cancelled schedule)

        Raises:
            NotFoundError: 스케줄이 없을 때 (When schedule not found)
            BadRequestError: 취소 불가 상태일 때 (When schedule cannot be cancelled)
        """
        schedule: Schedule | None = await schedule_repository.get_by_id_with_org(
            db, schedule_id, organization_id
        )
        if schedule is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")

        if schedule.status not in ("draft", "pending"):
            raise BadRequestError(
                "초안 또는 승인 대기 상태의 스케줄만 취소할 수 있습니다 "
                "(Only draft or pending schedules can be cancelled)"
            )

        schedule.status = "cancelled"
        await db.flush()
        await db.refresh(schedule)
        return schedule

    async def get_schedules(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Schedule], int]:
        """스케줄 목록을 필터링하여 페이지네이션 조회합니다.

        List schedules with filters and pagination.
        date_from/date_to 범위 필터가 있으면 work_date보다 우선합니다.
        (Date range filters take precedence over single work_date.)

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            date_from: 시작일 범위 필터, 선택 (Optional range start date)
            date_to: 종료일 범위 필터, 선택 (Optional range end date)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Schedule], int]: (스케줄 목록, 전체 개수)
                                             (List of schedules, total count)
        """
        return await schedule_repository.get_by_filters(
            db, organization_id, store_id, user_id, work_date,
            date_from, date_to, status, page, per_page,
        )

    async def get_schedule(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
    ) -> Schedule:
        """스케줄 상세 정보를 조회합니다.

        Get schedule detail.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule_id: 스케줄 UUID (Schedule UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Schedule: 스케줄 상세 (Schedule detail)

        Raises:
            NotFoundError: 스케줄이 없을 때 (When schedule not found)
        """
        schedule: Schedule | None = await schedule_repository.get_by_id_with_org(
            db, schedule_id, organization_id
        )
        if schedule is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")
        return schedule

    async def build_response(
        self,
        db: AsyncSession,
        schedule: Schedule,
    ) -> dict:
        """스케줄 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build schedule response dict with resolved names (store, user,
        shift, position, created_by, approved_by).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule: 스케줄 ORM 객체 (Schedule ORM object)

        Returns:
            dict: 관련 엔티티 이름이 포함된 응답 딕셔너리
                  (Response dict with resolved entity names)
        """
        # 매장 이름 조회 — Fetch store name
        store_result = await db.execute(select(Store.name).where(Store.id == schedule.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        # 사용자 이름 조회 — Fetch user name
        user_result = await db.execute(select(User.full_name).where(User.id == schedule.user_id))
        user_name: str = user_result.scalar() or "Unknown"

        # 시간대 이름 조회 — Fetch shift name (optional)
        shift_name: str | None = None
        if schedule.shift_id is not None:
            shift_result = await db.execute(select(Shift.name).where(Shift.id == schedule.shift_id))
            shift_name = shift_result.scalar()

        # 포지션 이름 조회 — Fetch position name (optional)
        position_name: str | None = None
        if schedule.position_id is not None:
            position_result = await db.execute(select(Position.name).where(Position.id == schedule.position_id))
            position_name = position_result.scalar()

        # 작성자 이름 조회 — Fetch creator name (optional)
        created_by_name: str | None = None
        if schedule.created_by is not None:
            created_by_result = await db.execute(select(User.full_name).where(User.id == schedule.created_by))
            created_by_name = created_by_result.scalar()

        # 승인자 이름 조회 — Fetch approver name (optional)
        approved_by_name: str | None = None
        if schedule.approved_by is not None:
            approved_by_result = await db.execute(select(User.full_name).where(User.id == schedule.approved_by))
            approved_by_name = approved_by_result.scalar()

        return {
            "id": str(schedule.id),
            "organization_id": str(schedule.organization_id),
            "store_id": str(schedule.store_id),
            "store_name": store_name,
            "user_id": str(schedule.user_id),
            "user_name": user_name,
            "shift_id": str(schedule.shift_id) if schedule.shift_id else None,
            "shift_name": shift_name,
            "position_id": str(schedule.position_id) if schedule.position_id else None,
            "position_name": position_name,
            "work_date": schedule.work_date,
            "start_time": self._format_time(schedule.start_time),
            "end_time": self._format_time(schedule.end_time),
            "status": schedule.status,
            "note": schedule.note,
            "created_by": str(schedule.created_by) if schedule.created_by else None,
            "created_by_name": created_by_name,
            "approved_by": str(schedule.approved_by) if schedule.approved_by else None,
            "approved_by_name": approved_by_name,
            "approved_at": schedule.approved_at,
            "work_assignment_id": str(schedule.work_assignment_id) if schedule.work_assignment_id else None,
            "created_at": schedule.created_at,
        }


    async def substitute_schedule(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
        new_user_id: UUID,
        requested_by: UUID,
    ) -> Schedule:
        """대타 처리 — 승인된 스케줄의 담당자를 변경합니다.

        Substitute schedule — Change the assigned user of an approved schedule.
        Records substitution in schedule_approvals for audit trail.

        Args:
            db: Async database session
            schedule_id: Schedule UUID
            organization_id: Organization UUID
            new_user_id: New user UUID (substitute)
            requested_by: User who requested the substitution
        """
        schedule = await schedule_repository.get_by_id_with_org(db, schedule_id, organization_id)
        if schedule is None:
            raise NotFoundError("스케줄을 찾을 수 없습니다 (Schedule not found)")

        if schedule.status != "approved":
            raise BadRequestError("승인된 스케줄만 대타 처리할 수 있습니다 (Only approved schedules can be substituted)")

        old_user_id = schedule.user_id
        schedule.user_id = new_user_id

        # 대타 이력 기록 — Record substitution in approvals
        await schedule_repository.create_approval(db, {
            "schedule_id": schedule.id,
            "action": "substitute",
            "user_id": requested_by,
            "note": f"대타: {old_user_id} → {new_user_id}",
        })

        await db.flush()
        await db.refresh(schedule)

        # 대타 알림 — Notify old and new users about substitution
        from app.services.notification_service import notification_service
        await notification_service.create_for_substitute(db, schedule, old_user_id, new_user_id)

        return schedule

    async def validate_overtime(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        work_date: date,
        hours: float,
    ) -> dict:
        """주간 초과근무 사전 검증.

        Pre-validate weekly overtime before creating a schedule.
        Returns warning info if adding these hours exceeds thresholds.
        """
        from app.models.attendance import Attendance
        from app.models.organization import LaborLawSetting
        import datetime as dt

        # 해당 주의 월~일 계산
        weekday = work_date.weekday()
        week_start = work_date - dt.timedelta(days=weekday)
        week_end = week_start + dt.timedelta(days=6)

        # 해당 주 기존 근무시간 합산
        result = await db.execute(
            select(Attendance.total_work_minutes)
            .where(
                Attendance.user_id == user_id,
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end,
            )
        )
        existing_minutes = sum(r or 0 for r in result.scalars().all())
        existing_hours = existing_minutes / 60
        total_hours = existing_hours + hours

        # 노동법 설정 조회 (매장 기준)
        max_weekly = 40  # 기본값
        law_result = await db.execute(
            select(LaborLawSetting)
            .where(LaborLawSetting.organization_id == organization_id)
            .limit(1)
        )
        law = law_result.scalar_one_or_none()
        if law:
            max_weekly = law.store_max_weekly or law.state_max_weekly or law.federal_max_weekly

        return {
            "user_id": str(user_id),
            "week_start": str(week_start),
            "week_end": str(week_end),
            "existing_hours": round(existing_hours, 1),
            "adding_hours": hours,
            "total_hours": round(total_hours, 1),
            "max_weekly_hours": max_weekly,
            "overtime": total_hours > max_weekly,
            "overtime_hours": round(max(0, total_hours - max_weekly), 1),
        }


# 싱글턴 인스턴스 — Singleton instance
schedule_service: ScheduleService = ScheduleService()
