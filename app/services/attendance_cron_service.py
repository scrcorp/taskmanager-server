"""Attendance Cron Service — 자동 상태 전환.

Eager 모델 전환 후 역할이 단순해졌다:
- attendance row 는 schedule 생성 시점에 이미 존재.
- API 응답 시점에 effective status 가 계산(upcoming → soon/late)돼서 UX 에 바로 반영.
- 이 cron 은 DB persist 용: upcoming 인데 이미 시간이 지나 late/no_show 로 굳어진
  row 를 실제로 해당 status 로 업데이트 한다. 정산/통계 쿼리가 실시간 계산 없이
  DB 만 봐도 되도록.

APScheduler 진입점은 `run_attendance_state_tick()`.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting


logger = logging.getLogger("uvicorn.error")

DEFAULT_LATE_BUFFER_MINUTES = 5


async def _persist_late_and_no_show(db: AsyncSession) -> tuple[int, int]:
    """미출근 attendance 의 status 를 시간 경과에 따라 late / no_show 로 승격.

    승격 대상 status: 'upcoming' 또는 이미 'late' (clock-in 안 한 상태).
    'working' / 'on_break' 등 clock-in 후 상태는 건드리지 않음.
    """
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    two_days_ago = today_utc - timedelta(days=2)

    rows = await db.execute(
        select(Attendance, Schedule, Store)
        .join(Schedule, Schedule.id == Attendance.schedule_id)
        .outerjoin(Store, Store.id == Attendance.store_id)
        .where(
            Attendance.status.in_(["upcoming", "late"]),
            Attendance.work_date >= two_days_ago,
            Attendance.work_date <= today_utc,
            Schedule.start_time.isnot(None),
        )
    )
    rows_list = list(rows.all())

    late_count = 0
    no_show_count = 0

    for att, sch, store in rows_list:
        tz_name = (store.timezone if store else None) or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        sched_start = datetime.combine(sch.work_date, sch.start_time, tzinfo=tz)
        sched_end = (
            datetime.combine(sch.work_date, sch.end_time, tzinfo=tz)
            if sch.end_time is not None else None
        )
        if sched_end is not None and sched_end <= sched_start:
            sched_end = sched_end + timedelta(days=1)

        # late_buffer setting per org/store
        try:
            raw = await resolve_setting(
                db,
                key="attendance.late_buffer_minutes",
                organization_id=att.organization_id,
                store_id=att.store_id,
            )
            late_buffer = int(raw) if raw is not None else DEFAULT_LATE_BUFFER_MINUTES
        except (SettingNotRegisteredError, TypeError, ValueError):
            late_buffer = DEFAULT_LATE_BUFFER_MINUTES

        # 1) sched_end 가 지났으면 무조건 no_show (upcoming/late 모두 해당)
        if sched_end is not None and now_utc >= sched_end:
            if att.status != "no_show":
                att.status = "no_show"
                anoms = list(att.anomalies or [])
                if "no_show" not in anoms:
                    anoms.append("no_show")
                att.anomalies = anoms or None
                no_show_count += 1
            continue

        # 2) 아직 sched_end 전이면 late_buffer 지났을 때 upcoming → late
        if att.status == "upcoming" and now_utc >= sched_start + timedelta(minutes=late_buffer):
            att.status = "late"
            anoms = list(att.anomalies or [])
            if "late" not in anoms:
                anoms.append("late")
            att.anomalies = anoms or None
            late_count += 1

    if late_count or no_show_count:
        await db.commit()
    return late_count, no_show_count


async def run_attendance_state_tick() -> None:
    """APScheduler에서 호출되는 진입점. 5분마다 실행(혹은 원하는 간격)."""
    try:
        async with async_session() as db:
            late_cnt, no_show_cnt = await _persist_late_and_no_show(db)
            if late_cnt or no_show_cnt:
                logger.info(
                    f"[attendance_cron] persist late={late_cnt} no_show={no_show_cnt}"
                )
    except Exception as e:
        logger.warning(f"[attendance_cron] tick failed: {e}")
