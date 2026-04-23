"""근태 관리 서비스 — 근태 및 QR 코드 비즈니스 로직.

Attendance Service — Business logic for attendance and QR code management.
Handles QR code generation, attendance scanning (clock-in/out, breaks),
admin attendance listing, and correction management.
"""

import secrets
from datetime import date, datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance, AttendanceCorrection, QRCode
from app.models.attendance_break import (
    BREAK_TYPE_PAID_SHORT,
    BREAK_TYPE_UNPAID_LONG,
    AttendanceBreak,
)
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.user import User
from app.repositories.attendance_repository import attendance_repository, qr_code_repository
from app.utils.exceptions import BadRequestError, NotFoundError


# 지각/조퇴/휴식 anomaly 임계치 (분 단위) — defaults
# late: clock_in > scheduled_start + LATE_BUFFER_MINUTES 이면 late 로 기록
# no_show: cron 이 scheduled_end 이 지났는데 attendance 없으면 생성
LATE_BUFFER_MINUTES = 0  # 0 = 1분이라도 늦으면 late
EARLY_LEAVE_THRESHOLD_MINUTES = 5


async def _find_schedule_for_attendance(
    db: AsyncSession,
    user_id: UUID,
    store_id: UUID,
    work_date: date,
) -> Schedule | None:
    """해당 user/store/date의 confirmed schedule 찾기 (있으면 1개)."""
    result = await db.execute(
        select(Schedule).where(
            Schedule.user_id == user_id,
            Schedule.store_id == store_id,
            Schedule.work_date == work_date,
            Schedule.status == "confirmed",
        ).limit(1)
    )
    return result.scalar_one_or_none()


def _add_anomaly(attendance: Attendance, code: str) -> None:
    """anomalies 배열에 중복 없이 추가."""
    current = list(attendance.anomalies or [])
    if code not in current:
        current.append(code)
    attendance.anomalies = current


class AttendanceService:
    """근태 관리 서비스.

    Attendance service handling QR code management, attendance scanning,
    listing, and correction workflows.
    """

    # === QR 코드 관리 (QR Code Management) ===

    async def create_qr_code(
        self,
        db: AsyncSession,
        store_id: UUID,
        created_by: UUID,
    ) -> QRCode:
        """새 QR 코드를 생성합니다. 기존 활성 QR은 비활성화됩니다.

        Generate a new QR code for a store. Deactivates any existing active QR codes.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            created_by: 생성자 UUID (Creator user UUID)

        Returns:
            QRCode: 생성된 QR 코드 (Created QR code)
        """
        try:
            # 기존 활성 QR 코드 비활성화 — Deactivate existing active QR codes for the store
            await qr_code_repository.deactivate_store_qr_codes(db, store_id)

            # 새 QR 코드 생성 — Generate new random 32-char hex code
            code: str = secrets.token_hex(16)

            qr: QRCode = await qr_code_repository.create_qr(
                db,
                {
                    "store_id": store_id,
                    "code": code,
                    "is_active": True,
                    "created_by": created_by,
                },
            )

            await db.commit()
            return qr
        except Exception:
            await db.rollback()
            raise

    async def regenerate_qr_code(
        self,
        db: AsyncSession,
        qr_id: UUID,
        created_by: UUID,
    ) -> QRCode:
        """기존 QR 코드를 재생성합니다.

        Regenerate a QR code by deactivating the old one and creating a new one.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr_id: 기존 QR 코드 UUID (Existing QR code UUID)
            created_by: 생성자 UUID (Creator user UUID)

        Returns:
            QRCode: 새로 생성된 QR 코드 (Newly created QR code)

        Raises:
            NotFoundError: QR 코드가 없을 때 (When QR code not found)
        """
        # 기존 QR 코드 조회 — Find existing QR code
        old_qr: QRCode | None = await qr_code_repository.get_by_id(db, qr_id)
        if old_qr is None:
            raise NotFoundError("QR code not found")

        # 기존 QR 비활성화 후 새 QR 생성 — Deactivate old and create new
        # Note: create_qr_code handles commit internally
        return await self.create_qr_code(db, old_qr.store_id, created_by)

    async def get_store_qr(
        self,
        db: AsyncSession,
        store_id: UUID,
    ) -> QRCode | None:
        """매장의 활성 QR 코드를 조회합니다.

        Get the active QR code for a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)

        Returns:
            QRCode | None: 활성 QR 코드 또는 None (Active QR code or None)
        """
        return await qr_code_repository.get_qr_by_store(db, store_id)

    async def build_qr_response(
        self,
        db: AsyncSession,
        qr: QRCode,
    ) -> dict:
        """QR 코드 응답 딕셔너리를 구성합니다.

        Build QR code response dict with resolved store name.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr: QR 코드 ORM 객체 (QR code ORM object)

        Returns:
            dict: 매장 이름이 포함된 QR 코드 응답 (QR response with store name)
        """
        # 매장 이름 조회 — Fetch store name
        store_result = await db.execute(select(Store.name).where(Store.id == qr.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        return {
            "id": str(qr.id),
            "store_id": str(qr.store_id),
            "store_name": store_name,
            "code": qr.code,
            "is_active": qr.is_active,
            "created_at": qr.created_at,
        }

    # === 근태 스캔 (Attendance Scanning) ===

    async def scan(
        self,
        db: AsyncSession,
        qr_code_str: str,
        user_id: UUID,
        organization_id: UUID,
        action: str,
        client_timezone: str | None = None,
        location: dict | None = None,
    ) -> Attendance:
        """QR 코드를 스캔하여 출퇴근/휴식을 기록합니다.

        Process a QR code scan for clock-in, break, or clock-out.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr_code_str: 스캔한 QR 코드 문자열 (Scanned QR code string)
            user_id: 사용자 UUID (User UUID)
            organization_id: 조직 UUID (Organization UUID)
            action: 동작 유형 — "clock_in"|"break_start"|"break_end"|"clock_out"
            client_timezone: 클라이언트 IANA 타임존 (Client IANA timezone)
            location: GPS 위치, 선택 (Optional GPS {lat, lng})

        Returns:
            Attendance: 업데이트된 근태 기록 (Updated attendance record)

        Raises:
            BadRequestError: QR 코드가 유효하지 않거나, 동작이 잘못된 경우
                             (Invalid QR code or invalid action for current state)
        """
        # QR 코드 검증 — Validate QR code exists and is active
        qr: QRCode | None = await qr_code_repository.get_qr_by_code(db, qr_code_str)
        if qr is None or not qr.is_active:
            raise BadRequestError("Invalid or inactive QR code")

        # 만료 여부 확인 — Check expiration
        if qr.expires_at is not None and datetime.now(timezone.utc) > qr.expires_at:
            raise BadRequestError("QR code has expired")

        now: datetime = datetime.now(timezone.utc)
        store_id: UUID = qr.store_id

        # 타임존 + 경계 시각 해석 — Resolve timezone and day boundary
        from app.utils.timezone import get_store_day_config, get_work_date, resolve_timezone
        store_tz, store_day_start = await get_store_day_config(db, store_id)
        effective_tz: str = resolve_timezone(client_timezone, store_tz)
        client_timezone = effective_tz
        today: date = get_work_date(effective_tz, store_day_start, now)

        # 오늘의 근태 기록 조회/생성 — Get or create today's attendance record
        attendance: Attendance | None = await attendance_repository.get_user_today(db, user_id, today)

        # 동작별 처리 — Process based on action type
        if action == "clock_in":
            if attendance is not None:
                raise BadRequestError(
                    "이미 오늘 출근 기록이 있습니다 (Already clocked in today)"
                )
            # 해당 날짜의 confirmed schedule 찾기 (있으면 link + late 체크)
            schedule = await _find_schedule_for_attendance(db, user_id, store_id, today)
            new_status = "working"
            anomalies: list[str] = []
            if schedule and schedule.start_time is not None:
                # store timezone 기준 schedule 시작 시각
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(effective_tz)
                scheduled_start = _dt.combine(today, schedule.start_time, tzinfo=tz)
                from datetime import timedelta as _td
                if now > scheduled_start + _td(minutes=LATE_BUFFER_MINUTES):
                    new_status = "late"
                    anomalies.append("late")
            attendance = await attendance_repository.create(
                db,
                {
                    "organization_id": organization_id,
                    "store_id": store_id,
                    "user_id": user_id,
                    "schedule_id": schedule.id if schedule else None,
                    "work_date": today,
                    "clock_in": now,
                    "clock_in_timezone": client_timezone,
                    "status": new_status,
                    "anomalies": anomalies or None,
                },
            )

        elif action == "break_start":
            if attendance is None:
                raise BadRequestError(
                    "먼저 출근해야 합니다 (Must clock in first)"
                )
            if attendance.status not in ("working", "late"):
                raise BadRequestError(
                    "현재 상태에서 휴식을 시작할 수 없습니다 (Cannot start break in current state)"
                )
            attendance.break_start = now
            attendance.status = "on_break"
            await db.flush()
            await db.refresh(attendance)

        elif action == "break_end":
            if attendance is None:
                raise BadRequestError(
                    "먼저 출근해야 합니다 (Must clock in first)"
                )
            if attendance.status != "on_break":
                raise BadRequestError(
                    "현재 휴식 중이 아닙니다 (Not currently on break)"
                )
            attendance.break_end = now
            attendance.status = "working"

            # 휴식 시간 계산 — Calculate break minutes
            if attendance.break_start is not None:
                break_delta = now - attendance.break_start
                attendance.total_break_minutes = int(break_delta.total_seconds() / 60)

            await db.flush()
            await db.refresh(attendance)

        elif action == "clock_out":
            if attendance is None:
                raise BadRequestError(
                    "먼저 출근해야 합니다 (Must clock in first)"
                )
            if attendance.status not in ("working", "late", "on_break"):
                raise BadRequestError(
                    "이미 퇴근 처리되었습니다 (Already clocked out)"
                )

            # 휴식 중이면 먼저 휴식 종료 — End break if currently on break
            if attendance.status == "on_break" and attendance.break_start is not None:
                attendance.break_end = now
                break_delta = now - attendance.break_start
                attendance.total_break_minutes = int(break_delta.total_seconds() / 60)

            attendance.clock_out = now
            attendance.clock_out_timezone = client_timezone
            attendance.status = "clocked_out"

            # 총 근무 시간 계산 — Calculate total work minutes
            if attendance.clock_in is not None:
                work_delta = now - attendance.clock_in
                attendance.total_work_minutes = int(work_delta.total_seconds() / 60)

            # ─── Anomaly 감지 ───
            schedule = None
            if attendance.schedule_id is not None:
                schedule_result = await db.execute(
                    select(Schedule).where(Schedule.id == attendance.schedule_id)
                )
                schedule = schedule_result.scalar_one_or_none()

            if schedule and schedule.end_time is not None:
                from datetime import datetime as _dt, timedelta as _td
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(effective_tz)
                scheduled_end = _dt.combine(today, schedule.end_time, tzinfo=tz)
                # 조퇴
                if now < scheduled_end - _td(minutes=EARLY_LEAVE_THRESHOLD_MINUTES):
                    _add_anomaly(attendance, "early_leave")
                # 초과근무
                if attendance.total_work_minutes is not None:
                    scheduled_minutes = (
                        (scheduled_end - _dt.combine(today, schedule.start_time, tzinfo=tz)).total_seconds() / 60
                    )
                    if attendance.total_work_minutes > scheduled_minutes + 30:
                        _add_anomaly(attendance, "overtime")

            # 휴식 미사용 + 연속 근무 임계치 초과 체크
            from app.repositories.break_rule_repository import break_rule_repository
            break_rule = await break_rule_repository.get_by_store(db, store_id)
            if break_rule and (attendance.total_break_minutes or 0) == 0:
                if (attendance.total_work_minutes or 0) > break_rule.max_continuous_minutes:
                    _add_anomaly(attendance, "no_break")

            await db.flush()
            await db.refresh(attendance)

        else:
            raise BadRequestError(
                f"Invalid action: {action}. Use: clock_in, break_start, break_end, clock_out"
            )

        try:
            await db.commit()
            return attendance
        except Exception:
            await db.rollback()
            raise

    # === 관리자 기능 (Admin Functions) ===

    async def get_attendances(
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
    ) -> tuple[Sequence[Attendance], int]:
        """근태 기록 목록을 필터링하여 페이지네이션 조회합니다.

        List attendance records with filters and pagination.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user filter)
            work_date: 근무일 필터, 선택 (Optional date filter)
            date_from: 시작일 필터, 선택 (Optional date range start)
            date_to: 종료일 필터, 선택 (Optional date range end)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Attendance], int]: (근태 목록, 전체 개수)
        """
        return await attendance_repository.get_by_filters(
            db, organization_id, store_id, user_id, work_date, date_from, date_to, status, page, per_page
        )

    async def get_attendance(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
    ) -> Attendance:
        """근태 기록 단건을 조회합니다.

        Get a single attendance record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Attendance: 근태 기록 (Attendance record)

        Raises:
            NotFoundError: 근태 기록이 없을 때 (When attendance not found)
        """
        attendance: Attendance | None = await attendance_repository.get_by_id_with_org(
            db, attendance_id, organization_id
        )
        if attendance is None:
            raise NotFoundError("Attendance record not found")
        return attendance

    async def correct_attendance(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        field_name: str,
        corrected_value: str,
        reason: str,
        corrected_by: UUID,
    ) -> AttendanceCorrection:
        """근태 기록을 수정하고 수정 이력을 생성합니다.

        Correct an attendance field and create a correction audit record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)
            organization_id: 조직 UUID (Organization UUID)
            field_name: 수정할 필드 (Field to correct)
            corrected_value: 수정 값 — ISO datetime (Corrected value)
            reason: 수정 사유 (Reason for correction)
            corrected_by: 수정자 UUID (Admin UUID)

        Returns:
            AttendanceCorrection: 생성된 수정 이력 (Created correction record)

        Raises:
            NotFoundError: 근태 기록이 없을 때 (When attendance not found)
            BadRequestError: 수정 불가 필드일 때 (When field cannot be corrected)
        """
        # 수정 가능한 필드 목록 — Allowed correctable fields
        allowed_fields: set[str] = {"clock_in", "clock_out", "break_start", "break_end"}
        if field_name not in allowed_fields:
            raise BadRequestError(
                f"Cannot correct field: {field_name}. Allowed: {', '.join(allowed_fields)}"
            )

        # 근태 기록 조회 — Fetch attendance record
        attendance: Attendance = await self.get_attendance(db, attendance_id, organization_id)

        # 기존 값 가져오기 — Get original value
        original_value: str | None = None
        original_dt = getattr(attendance, field_name, None)
        if original_dt is not None:
            original_value = original_dt.isoformat()

        # 수정 이력 생성 — Create correction record
        correction: AttendanceCorrection = await attendance_repository.create_correction(
            db,
            {
                "attendance_id": attendance_id,
                "field_name": field_name,
                "original_value": original_value,
                "corrected_value": corrected_value,
                "reason": reason,
                "corrected_by": corrected_by,
            },
        )

        # 근태 기록 업데이트 — Update attendance field with corrected value
        corrected_dt: datetime = datetime.fromisoformat(corrected_value)
        setattr(attendance, field_name, corrected_dt)

        # 시간 재계산 — Recalculate minutes if relevant
        if field_name in ("clock_in", "clock_out") and attendance.clock_in and attendance.clock_out:
            work_delta = attendance.clock_out - attendance.clock_in
            attendance.total_work_minutes = int(work_delta.total_seconds() / 60)

        if field_name in ("break_start", "break_end") and attendance.break_start and attendance.break_end:
            break_delta = attendance.break_end - attendance.break_start
            attendance.total_break_minutes = int(break_delta.total_seconds() / 60)

        await db.flush()

        # 근태 수정 알림 — Notify GM+ about attendance correction
        from app.services.notification_service import notification_service
        await notification_service.create_for_attendance_correction(
            db, attendance_id, organization_id, corrected_by, field_name
        )

        try:
            await db.commit()
            return correction
        except Exception:
            await db.rollback()
            raise

    async def get_corrections(
        self,
        db: AsyncSession,
        attendance_id: UUID,
    ) -> Sequence[AttendanceCorrection]:
        """근태 수정 이력을 조회합니다.

        Get correction history for an attendance record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)

        Returns:
            Sequence[AttendanceCorrection]: 수정 이력 목록 (List of corrections)
        """
        return await attendance_repository.get_corrections(db, attendance_id)

    async def _load_breaks_map(
        self,
        db: AsyncSession,
        attendance_ids: list[UUID],
    ) -> dict[UUID, list[AttendanceBreak]]:
        """여러 attendance 의 break 목록을 한 번에 로드합니다.

        Batch-load attendance_breaks rows for a list of attendance IDs.
        Returns a mapping attendance_id -> sorted list of AttendanceBreak rows.
        """
        if not attendance_ids:
            return {}
        result = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id.in_(attendance_ids))
            .order_by(AttendanceBreak.started_at.asc())
        )
        out: dict[UUID, list[AttendanceBreak]] = {}
        for br in result.scalars().all():
            out.setdefault(br.attendance_id, []).append(br)
        return out

    @staticmethod
    def _summarize_breaks(
        breaks: list[AttendanceBreak],
    ) -> tuple[int, int, int, list[dict]]:
        """break 목록을 paid/unpaid 집계 + paid_short 10분 초과분 + dict 리스트.

        Returns:
            (paid_total, unpaid_total, paid_overage, items)
            - paid_total: 모든 paid_short 세션 duration 합
            - unpaid_total: 모든 unpaid_long 세션 duration 합
            - paid_overage: 각 paid_short 세션별 (duration - 10, 0 하한) 합. 근무시간에서 추가 차감 대상.
            - items: 타임라인용 dict 목록
        """
        paid: int = 0
        unpaid: int = 0
        paid_overage: int = 0
        items: list[dict] = []
        for br in breaks:
            duration = br.duration_minutes or 0
            if br.ended_at is not None:
                if br.break_type == BREAK_TYPE_PAID_SHORT:
                    paid += duration
                    paid_overage += max(0, duration - 10)
                elif br.break_type == BREAK_TYPE_UNPAID_LONG:
                    unpaid += duration
            items.append({
                "id": str(br.id),
                "started_at": br.started_at,
                "ended_at": br.ended_at,
                "break_type": br.break_type,
                "duration_minutes": br.duration_minutes,
            })
        return paid, unpaid, paid_overage, items

    async def build_response(
        self,
        db: AsyncSession,
        attendance: Attendance,
        breaks: list[AttendanceBreak] | None = None,
    ) -> dict:
        """근태 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build attendance response dict with resolved entity names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance: 근태 ORM 객체 (Attendance ORM object)
            breaks: 미리 로드된 break 목록, 선택 (Preloaded attendance_breaks for batching)

        Returns:
            dict: 매장/사용자 이름이 포함된 응답 딕셔너리
                  (Response dict with store/user names)
        """
        # 매장 이름 + 타임존 조회 — Fetch store name & timezone
        store_result = await db.execute(
            select(Store.name, Store.timezone).where(Store.id == attendance.store_id)
        )
        store_row = store_result.one_or_none()
        store_name: str = (store_row[0] if store_row else None) or "Unknown"
        store_tz_name: str | None = store_row[1] if store_row else None

        # 사용자 이름 조회 — Fetch user name
        user_result = await db.execute(select(User.full_name).where(User.id == attendance.user_id))
        user_name: str = user_result.scalar() or "Unknown"

        # 연결된 스케줄 조회 (있으면) — scheduled_start/end 채움
        scheduled_start: datetime | None = None
        scheduled_end: datetime | None = None
        if attendance.schedule_id is not None:
            sch_result = await db.execute(
                select(Schedule.start_time, Schedule.end_time, Schedule.work_date)
                .where(Schedule.id == attendance.schedule_id)
            )
            sch_row = sch_result.one_or_none()
            if sch_row is not None:
                s_start_time, s_end_time, s_work_date = sch_row
                # store.timezone이 None이면 organization timezone 조회 (fallback)
                if store_tz_name is None:
                    from app.models.organization import Organization
                    org_result = await db.execute(
                        select(Organization.timezone)
                        .join(Store, Store.organization_id == Organization.id)
                        .where(Store.id == attendance.store_id)
                    )
                    store_tz_name = org_result.scalar() or "UTC"
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(store_tz_name)
                except Exception:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo("UTC")
                if s_start_time is not None:
                    scheduled_start = datetime.combine(s_work_date, s_start_time, tzinfo=tz)
                if s_end_time is not None:
                    scheduled_end = datetime.combine(s_work_date, s_end_time, tzinfo=tz)

        # break 로드 (미리 주입되지 않았다면 fetch) — Load breaks if not provided
        if breaks is None:
            br_result = await db.execute(
                select(AttendanceBreak)
                .where(AttendanceBreak.attendance_id == attendance.id)
                .order_by(AttendanceBreak.started_at.asc())
            )
            breaks = list(br_result.scalars().all())

        paid_break_minutes, unpaid_break_minutes, paid_overage_minutes, break_items = (
            self._summarize_breaks(breaks)
        )

        # store tz 기준 "HH:MM" display formatter — admin UI 가 브라우저 로컬 tz 변환
        # 없이 그대로 렌더할 수 있도록 pre-formatted 값 제공.
        # store.timezone 이 아직 없으면 organization tz, 그마저도 없으면 UTC fallback.
        display_tz_name: str | None = store_tz_name
        if display_tz_name is None:
            from app.models.organization import Organization
            org_result = await db.execute(
                select(Organization.timezone)
                .join(Store, Store.organization_id == Organization.id)
                .where(Store.id == attendance.store_id)
            )
            display_tz_name = org_result.scalar() or "UTC"
        try:
            from zoneinfo import ZoneInfo
            display_tz = ZoneInfo(display_tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            display_tz = ZoneInfo("UTC")

        def _display_store_tz(value: datetime | None) -> str | None:
            """UTC/offset-aware datetime → store tz 기준 HH:MM. None → None."""
            if value is None:
                return None
            try:
                return value.astimezone(display_tz).strftime("%H:%M")
            except Exception:
                return None

        # break 타임라인에 started_at/ended_at_display 추가
        for item in break_items:
            item["started_at_display"] = _display_store_tz(item.get("started_at"))
            item["ended_at_display"] = _display_store_tz(item.get("ended_at"))

        # 총 휴식시간 — prefer aggregated paid+unpaid (더 정확), fallback to legacy field
        aggregated_break_minutes: int | None = None
        if break_items:
            aggregated_break_minutes = paid_break_minutes + unpaid_break_minutes
        total_break_minutes: int | None = (
            aggregated_break_minutes if aggregated_break_minutes is not None
            else attendance.total_break_minutes
        )

        # 순 근무시간 계산 — Net work minutes
        # = total_work
        #   - unpaid_break 전체 (일 안 한 시간)
        #   - paid_short 10분 초과분 (10분은 유급, 초과는 비근무 처리)
        total_work = attendance.total_work_minutes or 0
        net_work_minutes: int | None = None
        if attendance.total_work_minutes is not None:
            net_work_minutes = max(
                0, total_work - unpaid_break_minutes - paid_overage_minutes
            )

        return {
            "id": str(attendance.id),
            "store_id": str(attendance.store_id),
            "store_name": store_name,
            "user_id": str(attendance.user_id),
            "user_name": user_name,
            "schedule_id": str(attendance.schedule_id) if attendance.schedule_id else None,
            "work_date": attendance.work_date,
            "clock_in": attendance.clock_in,
            "clock_in_display": _display_store_tz(attendance.clock_in),
            "clock_in_timezone": attendance.clock_in_timezone,
            "break_start": attendance.break_start,
            "break_end": attendance.break_end,
            "clock_out": attendance.clock_out,
            "clock_out_display": _display_store_tz(attendance.clock_out),
            "clock_out_timezone": attendance.clock_out_timezone,
            "scheduled_start": scheduled_start,
            "scheduled_start_display": _display_store_tz(scheduled_start),
            "scheduled_end": scheduled_end,
            "scheduled_end_display": _display_store_tz(scheduled_end),
            "status": attendance.status,
            "anomalies": attendance.anomalies,
            "total_work_minutes": attendance.total_work_minutes,
            "total_break_minutes": total_break_minutes,
            "paid_break_minutes": paid_break_minutes,
            "unpaid_break_minutes": unpaid_break_minutes,
            "paid_break_overage_minutes": paid_overage_minutes,
            "net_work_minutes": net_work_minutes,
            "breaks": break_items,
            "note": attendance.note,
            "created_at": attendance.created_at,
        }

    async def build_correction_response(
        self,
        db: AsyncSession,
        correction: AttendanceCorrection,
    ) -> dict:
        """수정 이력 응답 딕셔너리를 구성합니다.

        Build correction response dict with resolved corrector name.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            correction: 수정 이력 ORM 객체 (Correction ORM object)

        Returns:
            dict: 수정자 이름이 포함된 응답 딕셔너리
                  (Response dict with corrector name)
        """
        # 수정자 이름 조회 — Fetch corrector name
        user_result = await db.execute(
            select(User.full_name).where(User.id == correction.corrected_by)
        )
        corrected_by_name: str = user_result.scalar() or "Unknown"

        return {
            "id": str(correction.id),
            "field_name": correction.field_name,
            "original_value": correction.original_value,
            "corrected_value": correction.corrected_value,
            "reason": correction.reason,
            "corrected_by": str(correction.corrected_by),
            "corrected_by_name": corrected_by_name,
            "created_at": correction.created_at,
        }


    async def get_weekly_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID | None = None,
        store_id: UUID | None = None,
        week_date: date | None = None,
    ) -> list[dict]:
        """주간 근무시간 요약 — 사용자별 일일/주간 근무시간.

        Weekly work time summary — per-user daily and weekly totals.
        Computes net_work_minutes (total - break) per day and aggregates weekly.
        """
        import datetime as dt
        from sqlalchemy import func

        target_date = week_date or date.today()
        weekday = target_date.weekday()
        week_start = target_date - dt.timedelta(days=(weekday + 1) % 7)
        week_end = week_start + dt.timedelta(days=6)

        query = (
            select(
                Attendance.user_id,
                func.sum(Attendance.total_work_minutes).label("total_work"),
                func.sum(Attendance.total_break_minutes).label("total_break"),
                func.count(Attendance.id).label("days_worked"),
            )
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end,
            )
            .group_by(Attendance.user_id)
        )
        if user_id:
            query = query.where(Attendance.user_id == user_id)
        if store_id:
            query = query.where(Attendance.store_id == store_id)

        result = await db.execute(query)
        rows = result.all()

        # 사용자 이름 일괄 조회 — Batch load user names
        user_ids = [row.user_id for row in rows]
        names_result = await db.execute(
            select(User.id, User.full_name).where(User.id.in_(user_ids))
        )
        names_map = {row.id: row.full_name for row in names_result}

        summaries: list[dict] = []
        for row in rows:
            user_name = names_map.get(row.user_id) or "Unknown"
            total_work = row.total_work or 0
            total_break = row.total_break or 0
            net_minutes = max(0, total_work - total_break)
            summaries.append({
                "user_id": str(row.user_id),
                "user_name": user_name,
                "week_start": str(week_start),
                "week_end": str(week_end),
                "days_worked": row.days_worked,
                "total_work_minutes": total_work,
                "total_break_minutes": total_break,
                "net_work_minutes": net_minutes,
                "net_work_hours": round(net_minutes / 60, 1),
            })
        return summaries

    async def get_overtime_alerts(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        week_date: date | None = None,
    ) -> list[dict]:
        """주간 초과근무 경고 목록 조회.

        Get overtime alerts — users whose weekly total exceeds threshold.
        """
        import datetime as dt
        from sqlalchemy import func
        from app.models.organization import LaborLawSetting

        target_date = week_date or date.today()
        weekday = target_date.weekday()
        week_start = target_date - dt.timedelta(days=(weekday + 1) % 7)
        week_end = week_start + dt.timedelta(days=6)

        # 노동법 기준 조회
        max_weekly = 40
        law_result = await db.execute(
            select(LaborLawSetting)
            .where(LaborLawSetting.organization_id == organization_id)
            .limit(1)
        )
        law = law_result.scalar_one_or_none()
        if law:
            max_weekly = law.store_max_weekly or law.state_max_weekly or law.federal_max_weekly

        # 주간 근무시간 합산 (사용자별)
        query = (
            select(
                Attendance.user_id,
                func.sum(Attendance.total_work_minutes).label("total_minutes"),
            )
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end,
            )
            .group_by(Attendance.user_id)
        )
        if store_id:
            query = query.where(Attendance.store_id == store_id)

        result = await db.execute(query)
        rows = result.all()

        alerts: list[dict] = []
        for row in rows:
            total_minutes = row.total_minutes or 0
            total_hours = total_minutes / 60
            if total_hours > max_weekly:
                user_result = await db.execute(
                    select(User.full_name).where(User.id == row.user_id)
                )
                user_name = user_result.scalar() or "Unknown"
                alerts.append({
                    "user_id": str(row.user_id),
                    "user_name": user_name,
                    "week_start": str(week_start),
                    "week_end": str(week_end),
                    "total_hours": round(total_hours, 1),
                    "max_weekly_hours": max_weekly,
                    "overtime_hours": round(total_hours - max_weekly, 1),
                })
        return alerts


# 싱글턴 인스턴스 — Singleton instance
attendance_service: AttendanceService = AttendanceService()
