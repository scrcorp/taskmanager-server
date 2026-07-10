"""Attendance Lifecycle Service — schedule 상태 변경에 맞춰 attendance row 동기화.

Eager 모델: schedule이 존재하는 동안 그에 묶인 attendance row 가 하나 존재한다.
schedule이 생성/확정/거부/취소/삭제/전환 될 때 schedule_service 가 이 모듈의
함수들을 호출해서 attendance 를 최신 상태로 유지한다.

규칙:
- row 생성: status 는 현재 시각 + schedule.start_time + late_buffer 기반으로 결정
  (upcoming / late / no_show). 단 schedule.status 가 cancelled/rejected/deleted 면 "cancelled"
- row 복구: schedule 이 cancelled 에서 다시 살아날 때 (revert 등) status 재계산
- row 취소: schedule 이 cancelled/rejected/deleted 되면 status="cancelled"로 마킹
- row 보존: 물리 삭제하지 않음. 이력 유지.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
from app.utils.timezone import resolve_schedule_instants


# 기본 late buffer (setting 미존재 시 fallback)
DEFAULT_LATE_BUFFER_MINUTES = 5


async def _resolve_late_buffer(db: AsyncSession, organization_id: UUID, store_id: UUID | None) -> int:
    try:
        raw = await resolve_setting(
            db,
            key="attendance.late_buffer_minutes",
            organization_id=organization_id,
            store_id=store_id,
        )
        return int(raw) if raw is not None else DEFAULT_LATE_BUFFER_MINUTES
    except (SettingNotRegisteredError, TypeError, ValueError):
        return DEFAULT_LATE_BUFFER_MINUTES


async def _resolve_store_tz(db: AsyncSession, store_id: UUID | None) -> ZoneInfo:
    """store.timezone null이면 organization.timezone fallback. 없으면 UTC."""
    if store_id is None:
        return ZoneInfo("UTC")
    from app.utils.timezone import get_store_day_config
    try:
        tz_name, _ = await get_store_day_config(db, store_id)
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _compute_initial_status(
    schedule_status: str,
    work_date: date_cls,
    start_time: time | None,
    end_time: time | None,
    tz: ZoneInfo,
    late_buffer_min: int,
    now_utc: datetime,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[str, list[str] | None]:
    """schedule 상태 + 시각 조합으로 attendance.status 와 anomalies 계산."""
    if schedule_status in ("cancelled", "rejected", "deleted"):
        return "cancelled", None
    # 시간이 없으면 일단 upcoming
    if start_at is None and end_at is None and start_time is None and end_time is None:
        return "upcoming", None
    # start_at 우선, 없으면 combine 폴백 (store tz aware)
    sched_start, sched_end = resolve_schedule_instants(
        start_at=start_at, end_at=end_at, work_date=work_date,
        start_time=start_time, end_time=end_time, tz_name=tz.key,
    )

    # late/no_show 판정은 분 단위로만 (초 버림). clock_in/out 은 초까지 저장하되,
    # 초 차이로 정시 출근이 late 로 찍히지 않게 한다.
    now_min = now_utc.replace(second=0, microsecond=0)
    if sched_end and now_min >= sched_end:
        return "no_show", ["no_show"]
    if sched_start and now_min > sched_start + timedelta(minutes=late_buffer_min):
        return "late", ["late"]
    return "upcoming", None


async def ensure_attendance_for_schedule(
    db: AsyncSession,
    schedule: Schedule,
) -> Attendance:
    """Schedule 에 묶인 attendance row 를 생성/복구.

    - row 가 없으면 새로 생성.
    - row 가 이미 있고 status 가 "cancelled" 라면 (schedule 이 revert 된 경우)
      현재 시각 기준으로 status 재계산. clock_in 등 기록 필드는 건드리지 않음.
    - row 가 이미 있고 진행 중(working/on_break/late 등)이면 그대로 둠.
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule.id)
    )
    now = datetime.now(timezone.utc)
    tz = await _resolve_store_tz(db, schedule.store_id)
    buffer = await _resolve_late_buffer(db, schedule.organization_id, schedule.store_id)
    status, anomalies = _compute_initial_status(
        schedule.status,
        schedule.work_date,
        schedule.start_time,
        schedule.end_time,
        tz,
        buffer,
        now,
        schedule.start_at,
        schedule.end_at,
    )

    if existing is None:
        attendance = Attendance(
            organization_id=schedule.organization_id,
            store_id=schedule.store_id,
            user_id=schedule.user_id,
            schedule_id=schedule.id,
            work_date=schedule.work_date,
            status=status,
            anomalies=anomalies,
        )
        db.add(attendance)
        await db.flush()
        return attendance

    # 이미 존재: cancelled 였으면 부활, 그 외에는 손대지 않음 (clock-in 기록 보존)
    if existing.status == "cancelled":
        existing.status = status
        existing.anomalies = anomalies
    return existing


async def recompute_attendance_for_schedule_change(
    db: AsyncSession,
    schedule: Schedule,
) -> None:
    """Schedule.work_date / start_time / end_time 이 바뀐 후 attendance.status 재계산.

    clock_in 이 이미 기록된 row 는 건드리지 않는다 (출근 기록 보존). clock_in 이 없는
    upcoming/late/no_show row 는 새 시간 기준으로 다시 계산해서 cron 의 강등/승격
    동기화를 기다리지 않고 즉시 일관된 상태를 보장한다.
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule.id)
    )
    if existing is None or existing.clock_in is not None:
        return
    if existing.status not in ("upcoming", "late", "no_show"):
        return
    now = datetime.now(timezone.utc)
    tz = await _resolve_store_tz(db, schedule.store_id)
    buffer = await _resolve_late_buffer(db, schedule.organization_id, schedule.store_id)
    status, anomalies = _compute_initial_status(
        schedule.status,
        schedule.work_date,
        schedule.start_time,
        schedule.end_time,
        tz,
        buffer,
        now,
        schedule.start_at,
        schedule.end_at,
    )
    existing.status = status
    existing.anomalies = anomalies
    existing.work_date = schedule.work_date
    await db.flush()


async def cancel_attendance_for_schedule(
    db: AsyncSession,
    schedule_id: UUID,
) -> None:
    """Schedule 이 cancel/reject/delete 되었을 때 attendance.status 를 cancelled 로 마킹.

    row 가 없으면 아무것도 하지 않는다 (이미 정리되었거나 애초에 생성 안됨).
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule_id)
    )
    if existing is None:
        return
    existing.status = "cancelled"
    # 이전 anomalies 는 유지 (late 후 cancelled 같은 이력)
    await db.flush()


async def reassign_attendance_user(
    db: AsyncSession,
    schedule_id: UUID,
    new_user_id: UUID,
) -> None:
    """Switch Schedule 등으로 schedule.user_id 가 바뀌었을 때 attendance.user_id 동기화.

    진행 중 기록이 있어도 user_id 만 옮김 (clock-in 등은 원래 사람 것이라 일반적이면 switch
    전에 정리돼야 하지만 정책은 상위에서 결정; 여기서는 단순 동기화).
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule_id)
    )
    if existing is None:
        return
    existing.user_id = new_user_id
    await db.flush()
