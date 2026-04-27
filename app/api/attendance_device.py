"""Attendance Device 전용 라우터 — 매장 공용 기기 self-service API.

Separate auth scope from the JWT-based admin/app APIs. A physical terminal
registers once with an access code, stores the returned device token in
secure storage, and uses it for all subsequent clock actions.

Mounted at `/api/v1/attendance` in `app/main.py`.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.core.access_code import verify_code
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.models.organization import Organization, Store
from app.schemas.attendance_device import (
    AssignStoreRequest,
    AttendanceStoreOption,
    ClockActionRequest,
    DeviceMeResponse,
    NoticeRow,
    RegisterRequest,
    RegisterResponse,
    TodayStaffBreak,
    TodayStaffRow,
)
from app.services.attendance_device_service import attendance_device_service
from app.services.attendance_service import attendance_service

router: APIRouter = APIRouter()

ACCESS_CODE_SERVICE_KEY = "attendance"


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register_device(
    data: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RegisterResponse:
    """Access code 를 검증하고 새 기기 토큰을 발급."""
    # access_code 는 service_key 당 1개이며, organization 을 식별하지 않는다.
    # 현재 단일 조직 배포를 가정 — 없으면 400.
    if not await verify_code(db, ACCESS_CODE_SERVICE_KEY, data.access_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access code",
        )
    # 현재 시스템은 single-org 운영을 가정 (조직 1개 또는 대표 조직 1개).
    org_result = await db.execute(select(Organization).order_by(Organization.created_at).limit(1))
    organization = org_result.scalar_one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No organization configured",
        )
    device, token = await attendance_device_service.register(
        db, organization_id=organization.id, fingerprint=data.fingerprint
    )
    await db.commit()
    return RegisterResponse(
        token=token,
        device_id=device.id,
        device_name=device.device_name,
        store_id=device.store_id,
    )


@router.get("/me", response_model=DeviceMeResponse)
async def get_me(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceMeResponse:
    """현재 토큰의 기기 정보 — store tz 기준 work_date 포함."""
    from app.utils.timezone import get_store_day_config, get_work_date
    from datetime import datetime as _dt, timezone as _tz

    from zoneinfo import ZoneInfo

    store_name: str | None = None
    store_tz: str | None = None
    work_date_str: str | None = None
    offset_minutes: int | None = None
    if device.store_id is not None:
        store_result = await db.execute(select(Store).where(Store.id == device.store_id))
        store = store_result.scalar_one_or_none()
        store_name = store.name if store else None
        tz, day_start = await get_store_day_config(db, device.store_id)
        store_tz = tz
        now_utc = _dt.now(_tz.utc)
        wd = get_work_date(tz, day_start, now_utc)
        work_date_str = wd.isoformat()
        # 현재 시각의 store tz UTC offset (DST 반영). 분 단위.
        try:
            local = now_utc.astimezone(ZoneInfo(tz))
            off = local.utcoffset()
            if off is not None:
                offset_minutes = int(off.total_seconds() // 60)
        except Exception:
            offset_minutes = None
    return DeviceMeResponse(
        device_id=device.id,
        device_name=device.device_name,
        organization_id=device.organization_id,
        store_id=device.store_id,
        store_name=store_name,
        store_timezone=store_tz,
        store_timezone_offset_minutes=offset_minutes,
        work_date=work_date_str,
        registered_at=device.registered_at,
        last_seen_at=device.last_seen_at,
    )


@router.put("/store", response_model=DeviceMeResponse)
async def assign_store(
    data: AssignStoreRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceMeResponse:
    """매장 선택/변경 — 최초 setup 또는 Change Store 흐름."""
    from app.utils.timezone import get_store_day_config, get_work_date
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo

    await attendance_device_service.assign_store(db, device, data.store_id)
    await db.commit()
    store_result = await db.execute(select(Store).where(Store.id == device.store_id))
    store = store_result.scalar_one_or_none()

    store_tz: str | None = None
    offset_minutes: int | None = None
    work_date_str: str | None = None
    if device.store_id is not None:
        tz, day_start = await get_store_day_config(db, device.store_id)
        store_tz = tz
        now_utc = _dt.now(_tz.utc)
        work_date_str = get_work_date(tz, day_start, now_utc).isoformat()
        try:
            off = now_utc.astimezone(ZoneInfo(tz)).utcoffset()
            if off is not None:
                offset_minutes = int(off.total_seconds() // 60)
        except Exception:
            offset_minutes = None

    return DeviceMeResponse(
        device_id=device.id,
        device_name=device.device_name,
        organization_id=device.organization_id,
        store_id=device.store_id,
        store_name=store.name if store else None,
        store_timezone=store_tz,
        store_timezone_offset_minutes=offset_minutes,
        work_date=work_date_str,
        registered_at=device.registered_at,
        last_seen_at=device.last_seen_at,
    )


@router.get("/stores", response_model=list[AttendanceStoreOption])
async def list_stores(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AttendanceStoreOption]:
    """Device token 으로 조직 내 매장 후보 조회 (store select 화면용).

    기기는 JWT 가 없어 일반 store list API 를 호출할 수 없다. 등록된 organization
    내의 모든 매장 (soft-deleted 제외) 을 최소 정보만 반환.
    """
    result = await db.execute(
        select(Store)
        .where(
            Store.organization_id == device.organization_id,
            Store.deleted_at.is_(None),
        )
        .order_by(Store.name)
    )
    stores = result.scalars().all()
    return [AttendanceStoreOption(id=s.id, name=s.name) for s in stores]


@router.delete("/me", status_code=204)
async def unregister_device(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """기기 자체 해제."""
    await attendance_device_service.revoke(db, device)
    await db.commit()


async def _perform_action(
    db: AsyncSession,
    device: AttendanceDevice,
    pin: str,
    user_id: uuid.UUID,
    action: str,
    break_type: str | None = None,
) -> dict:
    attendance = await attendance_device_service.perform_clock_action(
        db,
        device=device,
        pin=pin,
        action=action,
        user_id=user_id,
        break_type=break_type,
    )
    return await attendance_service.build_response(db, attendance)


@router.post("/clock-in")
async def clock_in(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(db, device, data.pin, data.user_id, "clock_in")


@router.post("/clock-out")
async def clock_out(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(db, device, data.pin, data.user_id, "clock_out")


@router.post("/break-start")
async def break_start(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(
        db, device, data.pin, data.user_id, "break_start", break_type=data.break_type
    )


@router.post("/break-end")
async def break_end(
    data: ClockActionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await _perform_action(db, device, data.pin, data.user_id, "break_end")


# ── 대시보드 데이터 ────────────────────────────────────────


@router.get("/today-staff", response_model=list[TodayStaffRow])
async def today_staff(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TodayStaffRow]:
    """기기 매장 기준 오늘 스케줄 + 각 유저의 현재 attendance 상태.

    한 번의 호출로 On Shift / Coming Up / Completed 를 모두 반환. 클라이언트가
    status 로 분기해서 섹션에 배치.
    """
    if device.store_id is None:
        return []

    from datetime import date as date_cls, datetime as dt, timedelta, timezone as tz
    from zoneinfo import ZoneInfo

    from app.models.attendance import Attendance
    from app.models.attendance_break import (
        BREAK_TYPE_PAID_SHORT,
        BREAK_TYPE_UNPAID_LONG,
        AttendanceBreak,
    )
    from app.models.schedule import Schedule
    from app.models.user import User
    from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
    from app.utils.timezone import get_store_day_config, get_work_date

    now = dt.now(tz.utc)
    store_tz, store_day_start = await get_store_day_config(db, device.store_id)
    today: date_cls = get_work_date(store_tz, store_day_start, now)
    tz_info = ZoneInfo(store_tz)

    # ── Eager 모델: attendance row 가 진실원. schedule 은 LEFT JOIN.
    rows = await db.execute(
        select(Attendance, Schedule, User)
        .outerjoin(Schedule, Schedule.id == Attendance.schedule_id)
        .join(User, User.id == Attendance.user_id)
        .where(
            Attendance.store_id == device.store_id,
            Attendance.work_date == today,
            Attendance.status != "cancelled",
        )
    )
    triples: list[tuple[Attendance, Schedule | None, User]] = list(rows.all())
    if not triples:
        return []

    # break 요약
    att_ids = [a.id for (a, _s, _u) in triples]
    break_map: dict[uuid.UUID, list[AttendanceBreak]] = {}
    if att_ids:
        br_rows = await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id.in_(att_ids))
        )
        for br in br_rows.scalars().all():
            break_map.setdefault(br.attendance_id, []).append(br)

    # late_buffer 설정 — effective status 계산용
    organization_id = triples[0][0].organization_id
    try:
        late_buf_raw = await resolve_setting(
            db,
            key="attendance.late_buffer_minutes",
            organization_id=organization_id,
            store_id=device.store_id,
        )
        late_buffer = int(late_buf_raw) if late_buf_raw is not None else 5
    except (SettingNotRegisteredError, TypeError, ValueError):
        late_buffer = 5
    SOON_THRESHOLD_MINUTES = 5

    def combine(t):
        if t is None:
            return None
        return dt.combine(today, t, tzinfo=tz_info)

    def display_store_tz(value):
        if value is None:
            return None
        return value.astimezone(tz_info).strftime("%H:%M")

    def effective_status(att: Attendance, schedule: Schedule | None) -> str:
        """DB attendance.status + 현재 시각 + late_buffer 로 최종 표시 status 계산."""
        if att.status != "upcoming" or schedule is None or schedule.start_time is None:
            return att.status
        sched_start = dt.combine(schedule.work_date, schedule.start_time, tzinfo=tz_info)
        sched_end = (
            dt.combine(schedule.work_date, schedule.end_time, tzinfo=tz_info)
            if schedule.end_time is not None else None
        )
        if sched_end is not None and sched_end <= sched_start:
            sched_end = sched_end + timedelta(days=1)
        if sched_end is not None and now >= sched_end:
            return "no_show"
        if now >= sched_start + timedelta(minutes=late_buffer):
            return "late"
        if now >= sched_start - timedelta(minutes=SOON_THRESHOLD_MINUTES):
            return "soon"
        return "upcoming"

    result: list[TodayStaffRow] = []
    for att, schedule, user in triples:
        paid = unpaid = 0
        current: TodayStaffBreak | None = None
        for br in break_map.get(att.id, []):
            if br.ended_at is None:
                current = TodayStaffBreak(
                    started_at=br.started_at, break_type=br.break_type
                )
            else:
                if br.break_type == BREAK_TYPE_PAID_SHORT:
                    paid += br.duration_minutes or 0
                elif br.break_type == BREAK_TYPE_UNPAID_LONG:
                    unpaid += br.duration_minutes or 0

        sched_start = combine(schedule.start_time) if schedule else None
        sched_end = combine(schedule.end_time) if schedule else None
        result.append(
            TodayStaffRow(
                user_id=user.id,
                user_name=user.full_name or user.username,
                schedule_id=schedule.id if schedule else None,
                scheduled_start=sched_start,
                scheduled_end=sched_end,
                scheduled_start_display=display_store_tz(sched_start),
                scheduled_end_display=display_store_tz(sched_end),
                clock_in=att.clock_in,
                clock_out=att.clock_out,
                clock_in_display=display_store_tz(att.clock_in),
                clock_out_display=display_store_tz(att.clock_out),
                status=effective_status(att, schedule),
                current_break=current,
                paid_break_minutes=paid,
                unpaid_break_minutes=unpaid,
            )
        )

    # 정렬: working → on_break → soon → late → upcoming → clocked_out → no_show
    status_rank = {
        "working": 0, "on_break": 1, "soon": 2, "late": 3,
        "upcoming": 4, "clocked_out": 5, "no_show": 6,
    }

    def sort_key(row: TodayStaffRow):
        return (status_rank.get(row.status, 99), row.scheduled_start or datetime.max)

    result.sort(key=sort_key)
    return result


@router.get("/notices", response_model=list[NoticeRow])
async def notices(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 10,
) -> list[NoticeRow]:
    """기기 store 대상 공지 (최근 N개, 기본 10)."""
    from app.models.communication import Announcement

    from sqlalchemy import or_

    stmt = (
        select(Announcement)
        .where(
            Announcement.organization_id == device.organization_id,
            or_(
                Announcement.store_id.is_(None),
                Announcement.store_id == device.store_id,
            ),
        )
        .order_by(Announcement.created_at.desc())
        .limit(max(1, min(limit, 50)))
    )
    result = await db.execute(stmt)
    return [
        NoticeRow(
            id=a.id,
            title=a.title,
            body=a.content,
            created_at=a.created_at,
        )
        for a in result.scalars().all()
    ]
