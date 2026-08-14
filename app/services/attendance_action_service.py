"""콘솔 매니저용 attendance 상태 전이 서비스 — 의미 있는 액션 단위로만 변경.

Console-level attendance action service. State machine that enforces
invariants when admins modify attendance from the console:
- 각 액션은 "Clock In", "Start Break" 같은 의미 단위
- 관련 필드(예: clock_out + 진행중 break 종료) 가 함께 일관 업데이트
- AttendanceCorrection 행을 액션 이름으로 기록 (history 에 명확히 표시)

콘솔에서 status 를 직접 바꾸는 경로는 없어진다. 대신 이 서비스의 액션을 거친다.
"""

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance
from app.models.attendance_break import (
    VALID_BREAK_TYPES,
    AttendanceBreak,
    normalize_break_type,
)
from app.services import attendance_timeline as tl
from app.utils.exceptions import BadRequestError
from app.utils.timezone import minutes_between


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
        """org 격리 + 존재 검증 + L3 lock 가드된 attendance 반환.

        이 서비스의 모든 액션은 mutation 이므로 여기서 일괄 가드한다 —
        확정(confirmed)된 pay period 안의 근태는 상태 전이 불가 (409).
        """
        from app.services.attendance_service import attendance_service
        from app.services.payroll_lock_service import ensure_not_locked

        attendance = await attendance_service.get_attendance(
            db, attendance_id, organization_id
        )
        await ensure_not_locked(
            db, store_id=attendance.store_id, work_date=attendance.work_date
        )
        return attendance

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
            attendance.total_work_minutes = minutes_between(
                attendance.clock_in, attendance.clock_out
            )
        else:
            attendance.total_work_minutes = None

    async def _resolve_late_status(
        self, db: AsyncSession, attendance: Attendance, at: datetime
    ) -> tuple[str, list[str] | None]:
        """clock_in 시각 vs 스케줄 시작시간 → working / late 판정.

        스케줄이 없거나 start_time 이 없으면 working.

        임계값은 매장 설정(attendance.late_buffer_minutes) 이고, 판정은 공용
        순수 함수가 한다 — 예전엔 이 경로만 상수 0분 + **초를 살린 비교**라서
        17:05:30 이 지각으로 기록됐다(다른 경로는 분 절삭 + 설정 5분).
        """
        from app.models.schedule import Schedule
        from app.services.attendance_threshold_service import resolve_late_buffer
        from app.utils.attendance_judgement import is_late_arrival
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
        late_buffer = await resolve_late_buffer(
            db,
            organization_id=attendance.organization_id,
            store_id=attendance.store_id,
        )
        # at 이 UTC 인지 store-local 인지 정규화 — UTC 비교 기준으로 변환
        at_utc = at.astimezone(timezone.utc)
        scheduled_start_utc = scheduled_start.astimezone(timezone.utc)
        if is_late_arrival(at_utc, scheduled_start_utc, late_buffer=late_buffer):
            return "late", ["late"]
        return "working", None

    def _record_action(
        self,
        db: AsyncSession,
        *,
        attendance: Attendance,
        action: str,
        before_status: str | None,
        field_name: str | None = None,
        before: str | None = None,
        after: str | None = None,
        reason: str,
        by_user_id: UUID,
    ) -> None:
        """한 액션의 타임라인 행들을 남긴다 — status 전이 + (있으면) 값 전이.

        status 는 모든 액션에서 기록한다. "clock-in 이전 상태는 upcoming" 처럼
        이전 상태가 늘 존재하기 때문이다 (attendance_timeline 모듈 계약 참조).
        """
        group = tl.new_group()
        tl.record_status(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=action,
            before=before_status,
            after=attendance.status,
            reason=reason,
            by_user_id=by_user_id,
        )
        if field_name is not None:
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                field_name=field_name,
                before=before if before is not None else tl.NONE,
                after=after if after is not None else tl.NONE,
                reason=reason,
                by_user_id=by_user_id,
            )

    async def _commit_or_rollback(self, db: AsyncSession) -> None:
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ── 액션들 ──────────────────────────────────────────────────────────

    async def _refresh_overlap(self, db: AsyncSession, attendance: Attendance) -> None:
        """겹침 라벨 재계산 — 겹침을 만들거나 없앨 수 있는 모든 지점에서 부른다.

        콘솔 경로(이 서비스)에는 겹침 가드가 없어서 매니저가 두 shift 를 각각
        clock-in 시키면 라벨 없이 겹침이 만들어진다. 반대로 한쪽을 정정/취소하면
        반대편 라벨이 낡은 채 남는다. 그래서 "붙이기" 가 아니라 매번 다시 계산한다.
        """
        from app.services.attendance_service import refresh_overlap_anomaly

        await refresh_overlap_anomaly(
            db, user_id=attendance.user_id, work_date=attendance.work_date
        )

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

        from app.utils.timezone import get_store_day_config, interpret_clock_time

        before_status = attendance.status
        before_clock_in = attendance.clock_in
        store_tz, _ = await get_store_day_config(db, attendance.store_id)
        # (AK-1) naive 입력은 매장 타임존 벽시계로 해석 → UTC instant 저장
        at = interpret_clock_time(at, store_tz)
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

        self._record_action(
            db,
            attendance=attendance,
            action=tl.ACTION_CLOCK_IN,
            before_status=before_status,
            field_name=tl.FIELD_CLOCK_IN,
            before=tl.dt_value(before_clock_in),
            after=tl.dt_value(at),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._refresh_overlap(db, attendance)
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

        from app.utils.timezone import get_store_day_config, interpret_clock_time

        before_status = attendance.status
        before_clock_out = attendance.clock_out
        store_tz, _ = await get_store_day_config(db, attendance.store_id)
        # (AK-1) naive 입력은 매장 타임존 벽시계로 해석 → UTC instant 저장.
        # 비교 전에 정규화해야 naive vs aware 비교 오류도 안 난다.
        at = interpret_clock_time(at, store_tz)
        if at < attendance.clock_in:
            raise BadRequestError("Clock-out cannot be earlier than clock-in")

        # 진행중 break 가 있으면 같은 시각에 닫는다.
        open_break = await self._get_open_break(db, attendance.id)
        if open_break is not None:
            if at < open_break.started_at:
                raise BadRequestError(
                    "Clock-out cannot be earlier than the current break start"
                )
            open_break.ended_at = at
            open_break.duration_minutes = minutes_between(open_break.started_at, at)
            attendance.break_end = at

        attendance.clock_out = at
        attendance.clock_out_timezone = store_tz
        attendance.status = "clocked_out"
        self._recalc_total_work(attendance)
        attendance.total_break_minutes = await self._sum_break_minutes(db, attendance.id)

        # (L6) 자동퇴근 이력이 있는 record 에 사람이 clock-out 을 다시 기록하면
        # (reopen → clock-out 흐름) 확인(confirm)으로 간주 — corrected == human-verified.
        from app.services.attendance_service import attendance_service
        attendance_service._mark_auto_clock_out_confirmed_if_applicable(
            attendance, "clock_out", by_user_id
        )

        self._record_action(
            db,
            attendance=attendance,
            action=tl.ACTION_CLOCK_OUT,
            before_status=before_status,
            field_name=tl.FIELD_CLOCK_OUT,
            before=tl.dt_value(before_clock_out),
            after=tl.dt_value(at),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._refresh_overlap(db, attendance)
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

        from app.utils.timezone import get_store_timezone, interpret_clock_time

        # (AK-1) naive 입력은 매장 타임존 벽시계로 해석 → UTC instant 저장
        at = interpret_clock_time(at, await get_store_timezone(db, attendance.store_id))
        if attendance.clock_in is not None and at < attendance.clock_in:
            raise BadRequestError("Break cannot start before clock-in")
        open_break = await self._get_open_break(db, attendance.id)
        if open_break is not None:
            raise BadRequestError("A break is already in progress")

        before_status = attendance.status
        normalized = normalize_break_type(break_type)
        new_break = AttendanceBreak(
            attendance_id=attendance.id,
            started_at=at,
            break_type=normalized,
        )
        db.add(new_break)
        # id 는 컬럼 default(uuid4) 라 INSERT 전에는 None — 이력이 세션을 지목하려면
        # 먼저 flush 해서 PK 를 확정해야 한다.
        await db.flush()
        attendance.status = "on_break"
        attendance.break_start = at
        attendance.break_end = None

        group = tl.new_group()
        tl.record_status(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=tl.ACTION_BREAK_START,
            before=before_status,
            after=attendance.status,
            reason=reason,
            by_user_id=by_user_id,
        )
        tl.record_break_snapshot(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=tl.ACTION_BREAK_START,
            break_id=new_break.id,
            before=(None, None, None),
            after=(at, None, normalized),
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

        from app.utils.timezone import get_store_timezone, interpret_clock_time

        # (AK-1) naive 입력은 매장 타임존 벽시계로 해석 → UTC instant 저장
        at = interpret_clock_time(at, await get_store_timezone(db, attendance.store_id))
        if at < open_break.started_at:
            raise BadRequestError("Break end cannot be earlier than break start")

        before_status = attendance.status
        open_break.ended_at = at
        open_break.duration_minutes = minutes_between(open_break.started_at, at)
        attendance.status = "working"
        attendance.break_end = at
        attendance.total_break_minutes = await self._sum_break_minutes(db, attendance.id)

        group = tl.new_group()
        tl.record_status(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=tl.ACTION_BREAK_END,
            before=before_status,
            after=attendance.status,
            reason=reason,
            by_user_id=by_user_id,
        )
        tl.record(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=tl.ACTION_BREAK_END,
            field_name=tl.FIELD_BREAK_END_AT,
            before=tl.NONE,
            after=tl.dt_value(at),
            reason=reason,
            by_user_id=by_user_id,
            target_type=tl.TARGET_BREAK,
            target_id=open_break.id,
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

        self._record_action(
            db,
            attendance=attendance,
            action=tl.ACTION_NO_SHOW,
            before_status=original_status,
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance

    async def clear_times(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        reason: str,
        by_user_id: UUID,
    ) -> Attendance:
        """clock_in / clock_out / break 세션을 모두 지우고 "출근 전" 으로 되돌린다.

        존재 이유 — 이게 없으면 잘못 찍힌 clock_in 을 지울 방법이 아예 없다:
          - `mark_no_show` 는 시간 기록이 있으면 거부하며 "Reopen and clear first" 라고 안내
          - 그런데 `reopen` 은 clock_out 만 지운다 (clock_in 은 못 지움)
          - `correct` 는 corrected_value 가 필수 문자열이라 null 을 넣을 수 없다
        즉 서버가 자기 에러 문구에서 지시하는 동작을 수행할 경로가 없었다.

        지워진 값은 전부 타임라인에 before 로 남는다 — 기록이 조용히 증발하면 안 된다.
        정리 후 status 는 스케줄 기준으로 재판정되어 upcoming/late/no_show 중 하나가 되고,
        그 다음 `mark_no_show` 로 결번 처리할 수 있다.

        워크인(schedule 없는 row)은 대상이 아니다 — 시간을 지우면 남는 의미가 없다.
        """
        from sqlalchemy import select

        from app.services.attendance_lifecycle_service import status_after_time_clear

        attendance = await self._get_attendance(db, attendance_id, organization_id)

        if attendance.schedule_id is None:
            raise BadRequestError(
                "This record has no linked shift, so clearing its times would leave "
                "nothing behind. Cancel the record instead."
            )
        if attendance.clock_in is None and attendance.clock_out is None:
            breaks_exist = await db.scalar(
                select(AttendanceBreak.id)
                .where(AttendanceBreak.attendance_id == attendance.id)
                .limit(1)
            )
            if breaks_exist is None:
                raise BadRequestError("There are no time records to clear.")

        original_status = attendance.status
        original_in = attendance.clock_in
        original_out = attendance.clock_out

        breaks_result = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id == attendance.id)
            .order_by(AttendanceBreak.started_at)
        )
        breaks = list(breaks_result.scalars().all())

        attendance.clock_in = None
        attendance.clock_in_timezone = None
        attendance.clock_out = None
        attendance.clock_out_timezone = None
        attendance.break_start = None
        attendance.break_end = None
        attendance.total_work_minutes = None
        attendance.total_break_minutes = None
        # 확인 도장도 함께 지운다 — 지워진 시각에 대한 확인은 의미가 없고,
        # 남겨두면 payroll 게이트가 "확인됨" 으로 통과시켜 버린다.
        attendance.auto_clock_out_confirmed_at = None
        attendance.auto_clock_out_confirmed_by = None
        attendance.early_clock_in_confirmed_at = None
        attendance.early_clock_in_confirmed_by = None
        # 조기 출근 요청자도 같이 지운다 — 지워진 출근에 "누가 불렀나" 만 남으면
        # 유령이 되고, 나중에 다시 clock-in 했을 때 다른 사유에 그대로 붙는다.
        # (사유 문자열 자체는 attendance_corrections 에 append-only 로 남는다.)
        attendance.early_clock_in_requested_by = None

        for b in breaks:
            await db.delete(b)

        status, anomalies = await status_after_time_clear(db, attendance)
        attendance.status = status
        attendance.anomalies = anomalies

        # 한 액션이므로 모든 전이를 같은 group 으로 묶는다.
        group = tl.new_group()
        tl.record_status(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=tl.ACTION_CLEAR_TIMES,
            before=original_status,
            after=attendance.status,
            reason=reason,
            by_user_id=by_user_id,
        )
        for field, before_dt in (
            (tl.FIELD_CLOCK_IN, original_in),
            (tl.FIELD_CLOCK_OUT, original_out),
        ):
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=tl.ACTION_CLEAR_TIMES,
                field_name=field,
                before=tl.dt_value(before_dt),
                after=tl.NONE,
                reason=reason,
                by_user_id=by_user_id,
            )
        for b in breaks:
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=tl.ACTION_CLEAR_TIMES,
                field_name=tl.FIELD_BREAK_START_AT,
                before=tl.dt_value(b.started_at),
                after=tl.NONE,
                reason=reason,
                by_user_id=by_user_id,
                target_type=tl.TARGET_BREAK,
                target_id=b.id,
            )
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=tl.ACTION_CLEAR_TIMES,
                field_name=tl.FIELD_BREAK_END_AT,
                before=tl.dt_value(b.ended_at),
                after=tl.NONE,
                reason=reason,
                by_user_id=by_user_id,
                target_type=tl.TARGET_BREAK,
                target_id=b.id,
            )

        await db.flush()
        await self._refresh_overlap(db, attendance)
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

        self._record_action(
            db,
            attendance=attendance,
            action=tl.ACTION_CANCEL,
            before_status=original_status,
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._refresh_overlap(db, attendance)
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
        original_clock_out = attendance.clock_out

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

        # clock_out 을 지우는 reopen 이면 그 전이도 함께 남긴다 (지워진 값이 뭐였는지).
        self._record_action(
            db,
            attendance=attendance,
            action=tl.ACTION_REOPEN,
            before_status=original_status,
            field_name=tl.FIELD_CLOCK_OUT,
            before=tl.dt_value(original_clock_out),
            after=tl.dt_value(attendance.clock_out),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()
        await self._refresh_overlap(db, attendance)
        await db.flush()
        await self._commit_or_rollback(db)
        await db.refresh(attendance)
        return attendance


attendance_action_service = AttendanceActionService()
