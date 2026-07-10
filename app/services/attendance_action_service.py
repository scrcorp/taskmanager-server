"""콘솔 매니저용 attendance 상태 전이 서비스 — 의미 있는 액션 단위로만 변경.

Console-level attendance action service. State machine that enforces
invariants when admins modify attendance from the console:
- 각 액션은 "Clock In", "Start Break" 같은 의미 단위
- 관련 필드(예: clock_out + 진행중 break 종료) 가 함께 일관 업데이트
- AttendanceCorrection 행을 액션 이름으로 기록 (history 에 명확히 표시)

콘솔에서 status 를 직접 바꾸는 경로는 없어진다. 대신 이 서비스의 액션을 거친다.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance, AttendanceCorrection
from app.models.attendance_break import (
    VALID_BREAK_TYPES,
    AttendanceBreak,
    normalize_break_type,
)
from app.utils.exceptions import BadRequestError


class AttendanceActionService:
    """콘솔 attendance state-machine.

    각 메서드는 단일 attendance row 를 받아 의미 있는 전이를 수행한다.
    호출 측은 attendance_id + 시각 + reason 만 전달; pre-condition 검증 +
    연관 필드 갱신 + correction 기록은 서비스가 책임진다.
    """

    # ── helpers ────────────────────────────────────────────────────────

    async def _get_attendance(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
    ) -> Attendance:
        """org 격리 + 존재 검증된 attendance 반환."""
        from app.services.attendance_service import attendance_service

        return await attendance_service.get_attendance(
            db, attendance_id, organization_id
        )

    async def _get_open_break(
        self, db: AsyncSession, attendance_id: UUID
    ) -> AttendanceBreak | None:
        from sqlalchemy import select

        result = await db.execute(
            select(AttendanceBreak)
            .where(
                AttendanceBreak.attendance_id == attendance_id,
                AttendanceBreak.ended_at.is_(None),
            )
            .order_by(AttendanceBreak.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _sum_break_minutes(
        self, db: AsyncSession, attendance_id: UUID
    ) -> int:
        from sqlalchemy import func, select

        result = await db.execute(
            select(func.coalesce(func.sum(AttendanceBreak.duration_minutes), 0))
            .where(
                AttendanceBreak.attendance_id == attendance_id,
                AttendanceBreak.duration_minutes.is_not(None),
            )
        )
        return int(result.scalar_one() or 0)

    def _recalc_total_work(self, attendance: Attendance) -> None:
        """clock_in/out 둘 다 있으면 분 단위 재계산."""
        if attendance.clock_in is not None and attendance.clock_out is not None:
            delta = attendance.clock_out - attendance.clock_in
            attendance.total_work_minutes = max(0, int(delta.total_seconds() / 60))
        else:
            attendance.total_work_minutes = None

    async def _resolve_late_status(
        self, db: AsyncSession, attendance: Attendance, at: datetime
    ) -> tuple[str, list[str] | None]:
        """clock_in 시각 vs 스케줄 시작시간 → working / late 판정.

        스케줄이 없거나 start_time 이 없으면 working.
        """
        from app.models.schedule import Schedule
        from app.services.attendance_service import LATE_BUFFER_MINUTES
        from app.utils.timezone import get_store_day_config
        from sqlalchemy import select

        from app.utils.timezone import resolve_schedule_instants
        if attendance.schedule_id is None:
            return "working", None
        sch = await db.scalar(
            select(Schedule).where(Schedule.id == attendance.schedule_id)
        )
        if sch is None or (sch.start_at is None and sch.start_time is None):
            return "working", None
        store_tz, _ = await get_store_day_config(db, attendance.store_id)
        scheduled_start, _ = resolve_schedule_instants(
            start_at=sch.start_at, end_at=sch.end_at, work_date=sch.work_date,
            start_time=sch.start_time, end_time=sch.end_time, tz_name=store_tz,
        )
        if scheduled_start is None:
            return "working", None
        # at 이 UTC 인지 store-local 인지 정규화 — UTC 비교 기준으로 변환
        at_utc = at.astimezone(timezone.utc)
        scheduled_start_utc = scheduled_start.astimezone(timezone.utc)
        if at_utc > scheduled_start_utc + timedelta(minutes=LATE_BUFFER_MINUTES):
            return "late", ["late"]
        return "working", None

    def _add_correction(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        field_name: str,
        original_value: str | None,
        corrected_value: str,
        reason: str,
        by_user_id: UUID,
    ) -> None:
        """timeline 행 추가. reason 은 항상 비지 않은 값으로 들어옴 (라우터 단 보장)."""
        db.add(
            AttendanceCorrection(
                attendance_id=attendance_id,
                field_name=field_name,
                original_value=original_value,
                corrected_value=corrected_value,
                reason=reason,
                corrected_by=by_user_id,
            )
        )

    async def _commit_or_rollback(self, db: AsyncSession) -> None:
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ── 액션들 ──────────────────────────────────────────────────────────

    async def clock_in(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        at: datetime,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """clock_in 시각 설정 + 스케줄 기준 working/late 판정.

        - 이미 clocked_out 이면 reopen 액션을 써야 함 (자동 reopen 안 함)
        - 이미 clock_in 이 있으면 시간 정정 — correct_attendance(clock_in time) 을 사용
        """
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        if attendance.status == "clocked_out":
            raise BadRequestError(
                "Already clocked out. Use Reopen to undo first."
            )
        if attendance.clock_in is not None:
            raise BadRequestError(
                "Clock-in already recorded. Edit the time instead."
            )

        from app.utils.timezone import get_store_day_config

        store_tz, _ = await get_store_day_config(db, attendance.store_id)
        status_val, anomalies = await self._resolve_late_status(db, attendance, at)

        attendance.clock_in = at
        attendance.clock_in_timezone = store_tz
        attendance.status = status_val
        existing_anoms = [a for a in (attendance.anomalies or []) if a != "no_show"]
        if anomalies:
            for a in anomalies:
                if a not in existing_anoms:
                    existing_anoms.append(a)
        attendance.anomalies = existing_anoms or None
        self._recalc_total_work(attendance)

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="clock_in",
            original_value=None,
            corrected_value=at.isoformat(),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def clock_out(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        at: datetime,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """clock_out 설정 + 진행중 break 자동 종료 + status=clocked_out."""
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        if attendance.clock_in is None:
            raise BadRequestError("Cannot clock out without clock-in")
        if attendance.status == "clocked_out":
            raise BadRequestError("Already clocked out")
        if at < attendance.clock_in:
            raise BadRequestError("Clock-out cannot be earlier than clock-in")

        from app.utils.timezone import get_store_day_config

        store_tz, _ = await get_store_day_config(db, attendance.store_id)

        # 진행중 break 가 있으면 같은 시각에 닫는다.
        open_break = await self._get_open_break(db, attendance.id)
        if open_break is not None:
            if at < open_break.started_at:
                raise BadRequestError(
                    "Clock-out cannot be earlier than the current break start"
                )
            open_break.ended_at = at
            delta = at - open_break.started_at
            open_break.duration_minutes = max(0, int(delta.total_seconds() / 60))
            attendance.break_end = at

        attendance.clock_out = at
        attendance.clock_out_timezone = store_tz
        attendance.status = "clocked_out"
        self._recalc_total_work(attendance)
        attendance.total_break_minutes = await self._sum_break_minutes(db, attendance.id)

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="clock_out",
            original_value=None,
            corrected_value=at.isoformat(),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def start_break(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        at: datetime,
        break_type: str,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """새 break 열기 + status=on_break. working/late 일 때만 허용."""
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        if attendance.status not in ("working", "late"):
            raise BadRequestError(
                f"Cannot start a break in '{attendance.status}' state"
            )
        if break_type not in VALID_BREAK_TYPES:
            raise BadRequestError(
                "break_type required (paid_10min or unpaid_meal)"
            )
        if attendance.clock_in is not None and at < attendance.clock_in:
            raise BadRequestError("Break cannot start before clock-in")
        open_break = await self._get_open_break(db, attendance.id)
        if open_break is not None:
            raise BadRequestError("A break is already in progress")

        normalized = normalize_break_type(break_type)
        db.add(
            AttendanceBreak(
                attendance_id=attendance.id,
                started_at=at,
                break_type=normalized,
            )
        )
        attendance.status = "on_break"
        attendance.break_start = at
        attendance.break_end = None

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="break_start",
            original_value=None,
            corrected_value=normalized,
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def end_break(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        at: datetime,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """진행중 break 닫기 + status=working. on_break 일 때만 허용."""
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        if attendance.status != "on_break":
            raise BadRequestError("Not currently on break")
        open_break = await self._get_open_break(db, attendance.id)
        if open_break is None:
            # 상태 불일치 보정
            attendance.status = "working"
            await db.flush()
            raise BadRequestError(
                "No open break record found (status normalized to working)"
            )
        if at < open_break.started_at:
            raise BadRequestError("Break end cannot be earlier than break start")

        open_break.ended_at = at
        delta = at - open_break.started_at
        open_break.duration_minutes = max(0, int(delta.total_seconds() / 60))
        attendance.status = "working"
        attendance.break_end = at
        attendance.total_break_minutes = await self._sum_break_minutes(db, attendance.id)

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="break_end",
            original_value=None,
            corrected_value=at.isoformat(),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def mark_no_show(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """status=no_show + 시간/break 비우기.

        이미 출근한 적이 있으면 안 됨 (clock_in 이 있으면 reopen 후 다시 정리해야 함).
        upcoming/soon/late(미출근) 에서만 허용.
        """
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        if attendance.clock_in is not None or attendance.clock_out is not None:
            raise BadRequestError(
                "Cannot mark no-show: time records exist. Reopen and clear first."
            )
        if attendance.status == "no_show":
            raise BadRequestError("Already marked no-show")

        original_status = attendance.status
        attendance.status = "no_show"
        anoms = list(attendance.anomalies or [])
        if "no_show" not in anoms:
            anoms.append("no_show")
        attendance.anomalies = anoms or None

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="no_show",
            original_value=original_status,
            corrected_value="no_show",
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def cancel(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """status=cancelled. clock_in 이 없는 미래 / 미출근 시점에서만 허용."""
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        if attendance.clock_in is not None:
            raise BadRequestError(
                "Cannot cancel: shift already started. Reopen and clear first."
            )
        if attendance.status == "cancelled":
            raise BadRequestError("Already cancelled")

        original_status = attendance.status
        attendance.status = "cancelled"

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="cancel",
            original_value=original_status,
            corrected_value="cancelled",
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def reopen(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """이전 상태로 되돌리기. status 에 따라 다른 의미:

        - clocked_out → working/on_break (clock_out 제거)
        - no_show → upcoming (anomaly no_show 제거)
        - cancelled → upcoming
        """
        attendance = await self._get_attendance(db, attendance_id, organization_id)
        original_status = attendance.status

        if attendance.status == "clocked_out":
            attendance.clock_out = None
            attendance.clock_out_timezone = None
            attendance.total_work_minutes = None
            # 진행중 break 가 있으면 on_break, 아니면 working
            open_break = await self._get_open_break(db, attendance.id)
            attendance.status = "on_break" if open_break else "working"
        elif attendance.status == "no_show":
            attendance.status = "upcoming"
            anoms = [a for a in (attendance.anomalies or []) if a != "no_show"]
            attendance.anomalies = anoms or None
        elif attendance.status == "cancelled":
            attendance.status = "upcoming"
        else:
            raise BadRequestError(
                f"Cannot reopen from '{attendance.status}' state"
            )

        self._add_correction(
            db,
            attendance_id=attendance.id,
            field_name="reopen",
            original_value=original_status,
            corrected_value=attendance.status,
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance


attendance_action_service = AttendanceActionService()
