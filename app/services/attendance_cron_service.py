"""Attendance Cron Service — 자동 상태 전환.

매 1분마다 실행되어 다음 처리를 수행:
1. confirmed schedule인데 attendance가 없고 schedule.end_time이 지난 경우 → no_show 생성
2. (향후 확장) 추가 자동 전환 로직

APScheduler에서 호출되는 진입점은 `run_attendance_state_tick()`.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.attendance import Attendance
from app.models.schedule import Schedule


logger = logging.getLogger("uvicorn.error")


async def _process_no_shows(db: AsyncSession) -> int:
    """end_time이 지난 confirmed schedule 중 attendance 없는 건을 no_show로 생성."""
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo

    from app.utils.timezone import get_store_day_config

    now_utc = datetime.now(timezone.utc)
    created = 0

    today_utc = now_utc.date()
    two_days_ago = today_utc - _td(days=2)

    schedules_result = await db.execute(
        select(Schedule).where(
            Schedule.status == "confirmed",
            Schedule.work_date >= two_days_ago,
            Schedule.work_date <= today_utc,
            Schedule.end_time.isnot(None),
            Schedule.user_id.isnot(None),
            Schedule.store_id.isnot(None),
        )
    )
    schedules = list(schedules_result.scalars().all())

    for sch in schedules:
        # 해당 schedule_id에 이미 attendance가 있는지
        existing = await db.execute(
            select(Attendance).where(Attendance.schedule_id == sch.id)
        )
        if existing.scalar_one_or_none() is not None:
            continue

        # 같은 user/date에 attendance가 있는지 (schedule_id 없이)
        existing_by_user = await db.execute(
            select(Attendance).where(
                Attendance.user_id == sch.user_id,
                Attendance.work_date == sch.work_date,
            )
        )
        if existing_by_user.scalar_one_or_none() is not None:
            continue

        # store timezone 기준 schedule end 시각이 지났는지
        store_tz, _ = await get_store_day_config(db, sch.store_id)  # type: ignore[arg-type]
        try:
            tz = ZoneInfo(store_tz or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        scheduled_end = _dt.combine(sch.work_date, sch.end_time, tzinfo=tz)  # type: ignore[arg-type]
        if now_utc < scheduled_end:
            continue  # still ongoing

        attendance = Attendance(
            organization_id=sch.organization_id,
            store_id=sch.store_id,
            user_id=sch.user_id,
            schedule_id=sch.id,
            work_date=sch.work_date,
            status="no_show",
            anomalies=["no_show"],
        )
        db.add(attendance)
        created += 1

    if created > 0:
        await db.commit()
    return created


async def run_attendance_state_tick() -> None:
    """APScheduler에서 호출되는 진입점. 1분마다 실행."""
    try:
        async with async_session() as db:
            count = await _process_no_shows(db)
            if count > 0:
                logger.info(f"[attendance_cron] Created {count} no_show records")
    except Exception as e:
        logger.warning(f"[attendance_cron] tick failed: {e}")
