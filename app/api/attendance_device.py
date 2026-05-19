"""Attendance Device 전용 라우터 — 매장 공용 기기 self-service API.

Separate auth scope from the JWT-based admin/app APIs. A physical terminal
registers once with an access code, stores the returned device token in
secure storage, and uses it for all subsequent clock actions.

Mounted at `/api/v1/attendance` in `app/main.py`.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_attendance_admin_session,
    get_current_attendance_device,
)
from app.core.access_code import verify_code
from app.core.attendance_admin_session import (
    create_session as create_admin_session,
    revoke_session as revoke_admin_session,
)
from app.core.permissions import is_owner, is_sv_plus
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.schemas.app_version import AppVersionResponse
from app.schemas.attendance_device import (
    AdminAssignableUser,
    AdminClockActionRequest,
    AdminManagerOption,
    AdminScheduleCreateRequest,
    AdminScheduleRow,
    AdminScheduleUpdateRequest,
    AdminSessionRequest,
    AdminSessionResponse,
    AdminStatusChangeRequest,
    AdminWorkRoleOption,
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
from app.services.app_version_service import app_version_service
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
    reason: str | None = None,
) -> dict:
    attendance = await attendance_device_service.perform_clock_action(
        db,
        device=device,
        pin=pin,
        action=action,
        user_id=user_id,
        break_type=break_type,
        reason=reason,
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
    return await _perform_action(
        db, device, data.pin, data.user_id, "clock_out", reason=data.reason,
    )


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


# ── Tip entry (clock-out 통합) ─────────────────────────────


@router.post("/tip-entry", status_code=201)
async def device_tip_entry(
    data: dict,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """직원이 attendance device 에서 clock-out 직후 팁 입력.

    device token + 본인 PIN 으로 인증. tip_service.create_entry 호출.
    schedule_id 는 body 로 받거나, 가장 최근 attendance 의 schedule 로 자동 derive.

    body: {
        user_id, pin,
        schedule_id (optional — 없으면 자동 derive),
        card_tips, cash_tips_kept,
        distributions: [{receiver_id, amount, reason}],
    }
    """
    from app.models.attendance import Attendance
    from app.schemas.tip import TipEntryCreate
    from app.services.tip_service import tip_service

    if device.store_id is None:
        raise HTTPException(status_code=400, detail="Device has no store assigned")

    user_id_raw = data.get("user_id")
    pin = data.get("pin")
    if not user_id_raw or not pin:
        raise HTTPException(status_code=400, detail="user_id and pin required")

    user_id = uuid.UUID(str(user_id_raw))
    user = await attendance_device_service.verify_user_pin(
        db, user_id, str(pin), device.organization_id,
    )

    # schedule_id 자동 derive — body 우선, 없으면 user 의 가장 최근 attendance (clock-out
    # 직후 진입을 가정).
    schedule_id_raw = data.get("schedule_id")
    if schedule_id_raw:
        schedule_id = uuid.UUID(str(schedule_id_raw))
    else:
        latest_att = await db.scalar(
            select(Attendance)
            .where(
                Attendance.user_id == user_id,
                Attendance.store_id == device.store_id,
                Attendance.schedule_id.is_not(None),
            )
            .where(Attendance.clock_in.is_not(None)).order_by(Attendance.clock_in.desc())
            .limit(1)
        )
        if latest_att is None or latest_att.schedule_id is None:
            raise HTTPException(
                status_code=400,
                detail="Could not match this clock-out to a schedule. Use the staff app to submit.",
            )
        schedule_id = latest_att.schedule_id

    payload = TipEntryCreate(
        schedule_id=schedule_id,
        card_tips=data.get("card_tips", "0"),
        cash_tips_kept=data.get("cash_tips_kept", "0"),
        source="attendance",
        distributions=data.get("distributions", []),
    )
    entry = await tip_service.create_entry(db, actor=user, payload=payload)
    entry = await tip_service._get_entry_with_dists(db, entry.id)
    return tip_service.build_entry_response(
        entry, schedule=getattr(entry, "_schedule_loaded", None),
    )


@router.post("/tip-entry/eligible-receivers")
async def device_tip_eligible_receivers(
    data: dict,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """키오스크용 분배 후보 조회 — PIN 인증 후 같은 매장/같은 날/시간 겹친 staff.

    body: { user_id, pin, schedule_id (optional — 가장 최근 attendance 의 schedule 자동 derive) }
    """
    from app.models.attendance import Attendance
    from app.services.tip_service import tip_service

    if device.store_id is None:
        raise HTTPException(status_code=400, detail="Device has no store assigned")

    user_id_raw = data.get("user_id")
    pin = data.get("pin")
    if not user_id_raw or not pin:
        raise HTTPException(status_code=400, detail="user_id and pin required")

    user_id = uuid.UUID(str(user_id_raw))
    user = await attendance_device_service.verify_user_pin(
        db, user_id, str(pin), device.organization_id,
    )

    schedule_id_raw = data.get("schedule_id")
    if schedule_id_raw:
        schedule_id = uuid.UUID(str(schedule_id_raw))
    else:
        latest_att = await db.scalar(
            select(Attendance)
            .where(
                Attendance.user_id == user_id,
                Attendance.store_id == device.store_id,
                Attendance.schedule_id.is_not(None),
            )
            .where(Attendance.clock_in.is_not(None)).order_by(Attendance.clock_in.desc())
            .limit(1)
        )
        if latest_att is None or latest_att.schedule_id is None:
            raise HTTPException(
                status_code=400,
                detail="Could not match this clock-out to a schedule.",
            )
        schedule_id = latest_att.schedule_id

    return await tip_service.get_eligible_receivers(
        db,
        schedule_id=schedule_id,
        asking_user_id=user.id,
        organization_id=device.organization_id,
    )


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
        PAID_BREAK_TYPES,
        UNPAID_BREAK_TYPES,
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
        """DB attendance.status + 현재 시각 + late_buffer 로 최종 표시 status 계산.

        clock_in 이 이미 기록된 경우는 출근 완료 → DB status 그대로 (강등 금지).
        그 외 upcoming/late 미출근 상태는 schedule end 가 지나면 no_show 로 강등.
        """
        # 출근 후엔 시각과 무관하게 DB status 신뢰 — sched_end 지났다고 no_show로
        # 강등하면 늦게 clock-in 한 직원이 "Clocked In" 섹션에서 사라진다.
        if att.clock_in is not None:
            return att.status
        if att.status not in {"upcoming", "late"} or schedule is None or schedule.start_time is None:
            return att.status
        sched_start = dt.combine(schedule.work_date, schedule.start_time, tzinfo=tz_info)
        sched_end = (
            dt.combine(schedule.work_date, schedule.end_time, tzinfo=tz_info)
            if schedule.end_time is not None else None
        )
        if sched_end is not None and sched_end <= sched_start:
            sched_end = sched_end + timedelta(days=1)
        # 1) sched_end 지났으면 무조건 no_show
        if sched_end is not None and now >= sched_end:
            return "no_show"
        # 2) 이미 persisted 가 late 면 그대로 (시간이 sched_end 안 지난 경우만 도달)
        if att.status == "late":
            return "late"
        # 3) upcoming 인 경우 시간 비교로 분기
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
                if br.break_type in PAID_BREAK_TYPES:
                    paid += br.duration_minutes or 0
                elif br.break_type in UNPAID_BREAK_TYPES:
                    unpaid += br.duration_minutes or 0

        sched_start = combine(schedule.start_time) if schedule else None
        sched_end = combine(schedule.end_time) if schedule else None
        # Overnight shift (예: 21:00–02:00): end_time 이 start_time 보다 빠르면
        # sched_end 가 today 02:00 으로 만들어지는데 실제로는 다음날 02:00 이어야 한다.
        # early clock-out 사유 dialog 가 이 차이를 사용하므로 응답에서 보정.
        if sched_start is not None and sched_end is not None and sched_end <= sched_start:
            sched_end = sched_end + timedelta(days=1)
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
    from app.models.communication import Notice

    from sqlalchemy import or_

    stmt = (
        select(Notice)
        .where(
            Notice.organization_id == device.organization_id,
            or_(
                Notice.store_id.is_(None),
                Notice.store_id == device.store_id,
            ),
        )
        .order_by(Notice.created_at.desc())
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


# ── Kiosk 관리자 모드 ──────────────────────────────────────────────
# 매장 SV/GM/Owner 가 키오스크 설정에서 PIN 인증 후 사용. admin token 은 in-memory.
# 별도 라우터로 분리하지 않고 같은 prefix /attendance 아래 /admin/* 로 묶음.


@router.get("/admin/managers", response_model=list[AdminManagerOption])
async def admin_list_managers(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AdminManagerOption]:
    """현재 디바이스 매장에서 관리자 모드 진입 가능한 사용자 목록.

    포함: Owner (조직 내 모든 매장 관리) + 이 매장의 user_stores.is_manager=true 인 SV/GM.
    PIN 미설정자는 제외 (PIN 검증 불가).
    """
    if device.store_id is None:
        return []
    # Owners: 조직 내 priority == OWNER_PRIORITY
    from app.core.permissions import OWNER_PRIORITY, SV_PRIORITY

    owner_stmt = (
        select(User, Role)
        .join(Role, User.role_id == Role.id)
        .where(
            User.organization_id == device.organization_id,
            Role.priority == OWNER_PRIORITY,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            User.clockin_pin.is_not(None),
        )
    )
    # Store managers: SV+ 권한이면서 이 매장의 is_manager=true 인 user_stores
    manager_stmt = (
        select(User, Role)
        .join(Role, User.role_id == Role.id)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            User.organization_id == device.organization_id,
            UserStore.store_id == device.store_id,
            UserStore.is_manager.is_(True),
            Role.priority < OWNER_PRIORITY + 100,  # placeholder — 실제는 owner 제외 (아래)
            Role.priority <= SV_PRIORITY,
            Role.priority != OWNER_PRIORITY,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            User.clockin_pin.is_not(None),
        )
    )

    owners = (await db.execute(owner_stmt)).all()
    managers = (await db.execute(manager_stmt)).all()
    seen: set[uuid.UUID] = set()
    rows: list[AdminManagerOption] = []
    for user, role in list(owners) + list(managers):
        if user.id in seen:
            continue
        seen.add(user.id)
        rows.append(
            AdminManagerOption(
                user_id=user.id,
                full_name=user.full_name or user.username,
                role_name=role.name,
                role_priority=role.priority,
            )
        )
    rows.sort(key=lambda r: (r.role_priority, r.full_name))
    return rows


@router.post("/admin/session", response_model=AdminSessionResponse, status_code=201)
async def admin_open_session(
    data: AdminSessionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminSessionResponse:
    """매니저 user_id + PIN 검증 후 admin session token 발급."""
    if device.store_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Device has no store assigned")

    # PIN 검증 — verify_user_pin 은 active+org 체크 포함
    manager = await attendance_device_service.verify_user_pin(
        db, data.user_id, data.pin, device.organization_id
    )
    # 권한 검증: SV+ 이면서 owner 또는 이 매장 is_manager
    if manager.role is None or not is_sv_plus(manager):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized as manager",
        )
    if not is_owner(manager):
        us = await db.execute(
            select(UserStore).where(
                UserStore.user_id == manager.id,
                UserStore.store_id == device.store_id,
                UserStore.is_manager.is_(True),
            )
        )
        if us.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a manager of this store",
            )

    session = create_admin_session(
        device_id=device.id,
        manager_user_id=manager.id,
        organization_id=device.organization_id,
        store_id=device.store_id,
    )
    return AdminSessionResponse(
        admin_token=session.token,
        manager_user_id=manager.id,
        manager_name=manager.full_name or manager.username,
        expires_at=session.expires_at,
    )


@router.delete("/admin/session", status_code=204)
async def admin_close_session(
    request: Request,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
) -> None:
    """현재 admin session 종료 — UI Logout 버튼."""
    token = request.headers.get("X-Admin-Session") or request.headers.get("x-admin-session")
    revoke_admin_session(token)


# ── Admin Schedule CRUD ───────────────────────────────────


def _format_time_hhmm(t) -> str | None:
    if t is None:
        return None
    return t.strftime("%H:%M")


def _parse_time_hhmm(s: str):
    from datetime import time as _time
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


@router.get("/admin/schedules", response_model=list[AdminScheduleRow])
async def admin_list_today_schedules(
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AdminScheduleRow]:
    """현재 디바이스 매장의 오늘 스케줄 (status != cancelled/rejected/deleted)."""
    device, _session, _manager = auth
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo
    from app.models.attendance import Attendance
    from app.models.schedule import Schedule, StoreWorkRole
    from app.models.work import Shift
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))
    tz_info = ZoneInfo(store_tz)

    rows = await db.execute(
        select(Schedule, User, Attendance, Shift.name)
        .join(User, User.id == Schedule.user_id)
        .outerjoin(Attendance, Attendance.schedule_id == Schedule.id)
        .outerjoin(StoreWorkRole, StoreWorkRole.id == Schedule.work_role_id)
        .outerjoin(Shift, Shift.id == StoreWorkRole.shift_id)
        .where(
            Schedule.store_id == device.store_id,
            Schedule.work_date == today,
            Schedule.status.in_(("draft", "requested", "confirmed")),
        )
        .order_by(Schedule.start_time.asc().nulls_last(), User.full_name.asc())
    )

    def _display_tz(value):
        if value is None:
            return None
        try:
            return value.astimezone(tz_info).strftime("%H:%M")
        except Exception:
            return None

    result: list[AdminScheduleRow] = []
    for sched, user, att, shift_name in rows.all():
        result.append(
            AdminScheduleRow(
                schedule_id=sched.id,
                user_id=user.id,
                user_name=user.full_name or user.username,
                work_role_id=sched.work_role_id,
                work_role_name=sched.work_role_name_snapshot,
                shift_name=shift_name,
                position_name=sched.position_snapshot,
                start_time=_format_time_hhmm(sched.start_time),
                end_time=_format_time_hhmm(sched.end_time),
                status=sched.status,
                attendance_id=att.id if att else None,
                attendance_status=att.status if att else None,
                clock_in_display=_display_tz(att.clock_in) if att else None,
                clock_out_display=_display_tz(att.clock_out) if att else None,
            )
        )
    return result


@router.get("/admin/assignable-users", response_model=list[AdminAssignableUser])
async def admin_list_assignable_users(
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AdminAssignableUser]:
    """이 매장에 work_assignment 되어있는 직원들 (스케줄 생성 select 옵션)."""
    device, _session, _manager = auth
    rows = await db.execute(
        select(User, Role)
        .join(Role, User.role_id == Role.id)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            UserStore.store_id == device.store_id,
            UserStore.is_work_assignment.is_(True),
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
        .order_by(Role.priority.asc(), User.full_name.asc())
    )
    return [
        AdminAssignableUser(
            user_id=u.id,
            full_name=u.full_name or u.username,
            role_name=r.name,
        )
        for u, r in rows.all()
    ]


@router.get("/admin/work-roles", response_model=list[AdminWorkRoleOption])
async def admin_list_work_roles(
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AdminWorkRoleOption]:
    """매장 work role 목록 (스케줄 생성/수정 select 옵션).

    각 row 에 shift name + position name 도 함께 반환 — 클라이언트가
    "{shift} · {position}" 으로 합성해서 표시.
    """
    device, _session, _manager = auth
    from app.models.schedule import StoreWorkRole
    from app.models.work import Position, Shift

    rows = await db.execute(
        select(StoreWorkRole, Shift.name, Position.name)
        .outerjoin(Shift, Shift.id == StoreWorkRole.shift_id)
        .outerjoin(Position, Position.id == StoreWorkRole.position_id)
        .where(
            StoreWorkRole.store_id == device.store_id,
            StoreWorkRole.is_active.is_(True),
        )
        .order_by(StoreWorkRole.sort_order.asc())
    )
    return [
        AdminWorkRoleOption(
            work_role_id=wr.id,
            name=wr.name,
            shift_name=shift_name,
            position_name=pos_name,
            default_start_time=_format_time_hhmm(wr.default_start_time),
            default_end_time=_format_time_hhmm(wr.default_end_time),
        )
        for wr, shift_name, pos_name in rows.all()
    ]


async def _ensure_confirmed_today(db: AsyncSession, schedule_id: uuid.UUID, organization_id: uuid.UUID, manager_id: uuid.UUID) -> None:
    """create_entry 가 SV 권한 정책으로 requested 가 되어버린 경우 강제 confirmed.

    Kiosk admin 은 매니저가 직접 매장에서 즉시 운영을 하는 컨텍스트라 항상 confirmed.
    """
    from app.models.schedule import Schedule
    from app.services.schedule_service import schedule_service

    sch = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one_or_none()
    if sch is None:
        return
    if sch.status == "requested":
        await schedule_service.confirm_schedule(
            db, schedule_id, organization_id, approved_by=manager_id
        )


@router.post("/admin/schedules", response_model=AdminScheduleRow, status_code=201)
async def admin_create_schedule(
    data: AdminScheduleCreateRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminScheduleRow:
    """오늘 새 스케줄을 매장에 생성. 항상 confirmed."""
    device, _session, manager = auth
    from datetime import datetime as _dt, timezone as _tz
    from app.schemas.schedule import ScheduleCreate
    from app.services.schedule_service import schedule_service
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))

    payload = ScheduleCreate(
        store_id=str(device.store_id),
        user_id=str(data.user_id),
        work_role_id=str(data.work_role_id) if data.work_role_id else None,
        work_date=today,
        start_time=data.start_time,
        end_time=data.end_time,
        status="confirmed",
        force=True,
    )
    response = await schedule_service.create_entry(
        db, device.organization_id, payload, created_by=manager.id
    )
    # SV 매니저 권한이면 requested 로 떨어졌을 수 있음 → 강제 confirmed
    await _ensure_confirmed_today(db, uuid.UUID(response.id), device.organization_id, manager.id)
    # 재조회하여 응답 빌드
    return await _admin_schedule_row(db, uuid.UUID(response.id))


@router.patch("/admin/schedules/{schedule_id}", response_model=AdminScheduleRow)
async def admin_update_schedule(
    schedule_id: uuid.UUID,
    data: AdminScheduleUpdateRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminScheduleRow:
    """오늘 스케줄 시간/배정 수정. 매장+오늘 한정."""
    device, _session, manager = auth
    from app.models.schedule import Schedule
    from app.schemas.schedule import ScheduleUpdate
    from app.services.schedule_service import schedule_service

    sch = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one_or_none()
    if sch is None or sch.store_id != device.store_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    # 오늘만 허용
    from datetime import datetime as _dt, timezone as _tz
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))
    if sch.work_date != today:
        raise HTTPException(status_code=400, detail="Only today's schedule can be edited from kiosk")

    payload = ScheduleUpdate(
        user_id=str(data.user_id) if data.user_id else None,
        work_role_id=str(data.work_role_id) if data.work_role_id else None,
        start_time=data.start_time,
        end_time=data.end_time,
        force=True,
        reset_checklist=True,
    )
    await schedule_service.update_entry(
        db, schedule_id, device.organization_id, payload, actor=manager
    )
    return await _admin_schedule_row(db, schedule_id)


@router.delete("/admin/schedules/{schedule_id}", status_code=204)
async def admin_delete_schedule(
    schedule_id: uuid.UUID,
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """오늘 스케줄 삭제 — attendance 도 hard delete.

    Console 의 schedule delete 는 attendance 를 cancelled 로 마킹하지만, kiosk admin
    delete 는 매니저가 매장에서 즉시 "없던 일로 한다"는 명확한 의도. attendance row 와
    연결된 breaks/corrections 모두 cascade 로 정리하고 schedule 만 soft delete.
    """
    device, _session, manager = auth
    from app.models.attendance import Attendance
    from app.models.schedule import Schedule
    from app.services.schedule_service import schedule_service

    sch = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one_or_none()
    if sch is None or sch.store_id != device.store_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    from datetime import datetime as _dt, timezone as _tz
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))
    if sch.work_date != today:
        raise HTTPException(status_code=400, detail="Only today's schedule can be deleted from kiosk")

    # 1) attendance row hard delete (FK CASCADE 로 attendance_breaks /
    #    attendance_corrections 도 함께 정리). schedule_service.delete_entry 가
    #    cancel_attendance_for_schedule 를 호출해 status=cancelled 로 마킹하려 하지만
    #    row 가 이미 없으면 no-op 처리되어 안전.
    att = (await db.execute(
        select(Attendance).where(Attendance.schedule_id == schedule_id)
    )).scalar_one_or_none()
    if att is not None:
        await db.delete(att)
        await db.flush()

    # 2) schedule soft delete (status='deleted') — 기존 audit 정책 유지.
    await schedule_service.delete_entry(
        db, schedule_id, device.organization_id, actor=manager
    )


async def _admin_schedule_row(db: AsyncSession, schedule_id: uuid.UUID) -> AdminScheduleRow:
    """단일 schedule_id → AdminScheduleRow 빌드."""
    from zoneinfo import ZoneInfo
    from app.models.attendance import Attendance
    from app.models.schedule import Schedule, StoreWorkRole
    from app.models.work import Shift
    from app.utils.timezone import get_store_day_config

    row = (await db.execute(
        select(Schedule, User, Attendance, Shift.name)
        .join(User, User.id == Schedule.user_id)
        .outerjoin(Attendance, Attendance.schedule_id == Schedule.id)
        .outerjoin(StoreWorkRole, StoreWorkRole.id == Schedule.work_role_id)
        .outerjoin(Shift, Shift.id == StoreWorkRole.shift_id)
        .where(Schedule.id == schedule_id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    sched, user, att, shift_name = row

    tz_name, _ = await get_store_day_config(db, sched.store_id)
    tz_info = ZoneInfo(tz_name)

    def _display_tz(value):
        if value is None:
            return None
        try:
            return value.astimezone(tz_info).strftime("%H:%M")
        except Exception:
            return None

    return AdminScheduleRow(
        schedule_id=sched.id,
        user_id=user.id,
        user_name=user.full_name or user.username,
        work_role_id=sched.work_role_id,
        work_role_name=sched.work_role_name_snapshot,
        shift_name=shift_name,
        position_name=sched.position_snapshot,
        start_time=_format_time_hhmm(sched.start_time),
        end_time=_format_time_hhmm(sched.end_time),
        status=sched.status,
        attendance_id=att.id if att else None,
        attendance_status=att.status if att else None,
        clock_in_display=_display_tz(att.clock_in) if att else None,
        clock_out_display=_display_tz(att.clock_out) if att else None,
    )


# ── Admin Attendance Override ─────────────────────────────


@router.post("/admin/clock")
async def admin_clock_action(
    data: AdminClockActionRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """매니저가 임의 사용자 attendance 를 PIN 없이 처리.

    actions: clock_in | clock_out | break_start | break_end | cancel_clock_in | cancel_clock_out
    cancel_clock_in:  잘못 찍힌 출근을 초기화 (clock_in=NULL, status→upcoming).
    cancel_clock_out: 잘못 찍힌 퇴근을 되돌림 (clock_out=NULL, status→working).

    `reason` 은 attendance_corrections.reason 으로 그대로 저장된다. 매니저가
    별도 사유를 적도록 클라이언트가 강제하는 게 원칙. 라우터에서는 reason 을
    덮어쓰지 않는다.
    """
    device, _session, manager = auth
    action = data.action
    # reason 은 선택. 비어있으면 placeholder 로 기록 — 매니저가 나중에 console 에서
    # 수정 가능. attendance_corrections.reason 컬럼이 NOT NULL 이라 빈 문자열을 피한다.
    reason = (data.reason or "").strip() or "(no reason provided)"

    # 가드: 이 사용자의 오늘 attendance row 가 죽은 schedule(deleted/cancelled/rejected)에
    # 묶여있으면 admin override 거부. 카드에서 사라진 schedule 을 뒷문으로 살리는 걸 막는다.
    await _ensure_active_schedule_for_user(db, device, data.user_id)

    if action == "cancel_clock_in":
        return await _admin_cancel_clock_in(db, device, data.user_id, manager, reason)
    if action == "cancel_clock_out":
        return await _admin_cancel_clock_out(db, device, data.user_id, manager, reason)

    valid_actions = {"clock_in", "clock_out", "break_start", "break_end"}
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # service 가 모든 attendance 액션을 attendance_corrections 에 자동 기록.
    # reason 은 service 가 actor 라벨로 생성하지만, 매니저가 textfield 에 입력했으면
    # 그 값을 reason 으로 전달해 service 가 우선 사용.
    attendance = await attendance_device_service.perform_clock_action_admin(
        db,
        device=device,
        action=action,
        user_id=data.user_id,
        break_type=data.break_type,
        reason=reason if reason != "(no reason provided)" else None,
        manager_user_id=manager.id,
    )

    response = await attendance_service.build_response(db, attendance)
    return response


@router.post("/admin/attendance/status")
async def admin_change_attendance_status(
    data: AdminStatusChangeRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_admin_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """관리자가 attendance status 를 직접 변경 + 필요한 시각 보정.

    - status: working | late | on_break | clocked_out | upcoming | no_show
    - clock_in_hhmm / clock_out_hhmm: 변경하려는 시각 (store tz 기준 "HH:mm").
      해당 status 의 표준 동작과 일치하지 않는 입력은 무시되지 않고 그대로 반영
      (관리자가 명시적으로 시각을 지정한 것을 신뢰).
    - reason: attendance_corrections.reason 으로 기록 (필수).
    """
    from datetime import datetime as _dt, time as _t, timezone as _tz
    from zoneinfo import ZoneInfo
    from app.models.attendance import Attendance, AttendanceCorrection
    from app.repositories.attendance_repository import attendance_repository
    from app.utils.timezone import get_store_day_config, get_work_date

    device, _session, manager = auth
    allowed = {"working", "late", "on_break", "clocked_out", "upcoming", "no_show", "soon"}
    if data.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(allowed))}",
        )
    reason = (data.reason or "").strip() or "(no reason provided)"

    store_tz_name, day_start = await get_store_day_config(db, device.store_id)
    tz_info = ZoneInfo(store_tz_name)
    today = get_work_date(store_tz_name, day_start, _dt.now(_tz.utc))

    # 가드: 죽은 schedule(deleted/cancelled/rejected)을 뒷문으로 살리지 못하게.
    await _ensure_active_schedule_for_user(db, device, data.user_id)

    day_rows = await attendance_repository.list_user_day(db, data.user_id, today)
    target: Attendance | None = next(
        (r for r in day_rows if r.store_id == device.store_id), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="No attendance row for today")

    def _combine(hhmm: str):
        hh, mm = hhmm.split(":")
        return _dt.combine(today, _t(int(hh), int(mm)), tzinfo=tz_info)

    # ── 시간 보정 (요청 본문 기반) ──
    corrections_to_add: list[AttendanceCorrection] = []

    new_clock_in = target.clock_in
    new_clock_out = target.clock_out
    if data.clock_in_hhmm is not None:
        new_clock_in = _combine(data.clock_in_hhmm)
    if data.clock_out_hhmm is not None:
        new_clock_out = _combine(data.clock_out_hhmm)

    # ── status 별 정책 ──
    new_status = data.status
    if new_status in ("upcoming", "no_show"):
        # 출근 사실을 지움
        new_clock_in = None
        new_clock_out = None
    elif new_status in ("working", "late", "on_break"):
        # clock_in 없이는 working/late/on_break 불가 — 출근 사실이 있어야 한다.
        if new_clock_in is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{new_status} requires clock_in. Provide clock_in_hhmm or "
                    "use the Clock In action."
                ),
            )
        # clock_out 은 정리 (working/break 로 돌리려면 퇴근 기록 없어야 함)
        new_clock_out = None
    elif new_status == "clocked_out":
        # clock_in 없이 clocked_out 은 비논리적 → 거부
        if new_clock_in is None:
            raise HTTPException(
                status_code=400,
                detail="clocked_out requires clock_in. Provide clock_in_hhmm.",
            )
        if new_clock_out is None:
            new_clock_out = _dt.now(_tz.utc)

    # diff & corrections — 실제 변경된 필드만 기록
    if (target.clock_in or None) != new_clock_in:
        corrections_to_add.append(AttendanceCorrection(
            attendance_id=target.id,
            field_name="modify",
            original_value=(target.clock_in.isoformat() if target.clock_in else None) or "(none)",
            corrected_value=(new_clock_in.isoformat() if new_clock_in else "(cleared)"),
            reason=f"Clock-in time: {reason}",
            corrected_by=manager.id,
        ))
        target.clock_in = new_clock_in
        target.clock_in_timezone = store_tz_name if new_clock_in else None
    if (target.clock_out or None) != new_clock_out:
        corrections_to_add.append(AttendanceCorrection(
            attendance_id=target.id,
            field_name="modify",
            original_value=(target.clock_out.isoformat() if target.clock_out else None) or "(none)",
            corrected_value=(new_clock_out.isoformat() if new_clock_out else "(cleared)"),
            reason=f"Clock-out time: {reason}",
            corrected_by=manager.id,
        ))
        target.clock_out = new_clock_out
        target.clock_out_timezone = store_tz_name if new_clock_out else None
    if target.status != new_status:
        corrections_to_add.append(AttendanceCorrection(
            attendance_id=target.id,
            field_name="modify",
            original_value=target.status,
            corrected_value=new_status,
            reason=f"Status: {reason}",
            corrected_by=manager.id,
        ))
        target.status = new_status

    # 파생값 재계산
    if target.clock_in is not None and target.clock_out is not None:
        delta = target.clock_out - target.clock_in
        target.total_work_minutes = max(0, int(delta.total_seconds() / 60))
    else:
        target.total_work_minutes = None

    # status 가 출근 전(upcoming/no_show) 으로 가면 anomalies 도 정리
    if new_status in ("upcoming", "no_show"):
        target.anomalies = None
    elif new_status == "working":
        # early_clock_out / late 등 마무리 anomaly 정리
        anoms = [a for a in (target.anomalies or []) if a not in ("early_clock_out",)]
        target.anomalies = anoms or None

    for c in corrections_to_add:
        db.add(c)

    await db.flush()
    response = await attendance_service.build_response(db, target)
    await db.commit()
    return response


async def _ensure_active_schedule_for_user(
    db: AsyncSession, device: AttendanceDevice, user_id: uuid.UUID
) -> None:
    """이 사용자의 오늘 매장 schedule 중 살아있는(active) 것이 1건 이상 있는지 확인.

    active = status in ('draft','requested','confirmed'). 모두 deleted/cancelled/rejected 면
    400 — Edit/Status/Reopen 등 어떤 admin override 도 거부한다. "지운 스케줄을 뒷문으로
    살리는" 시나리오를 막는 가드.
    """
    from datetime import datetime as _dt, timezone as _tz
    from app.models.schedule import Schedule
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))
    row = await db.scalar(
        select(Schedule.id).where(
            Schedule.user_id == user_id,
            Schedule.store_id == device.store_id,
            Schedule.work_date == today,
            Schedule.status.in_(("draft", "requested", "confirmed")),
        )
    )
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This staff has no active schedule for today. "
                "Add a new schedule first if you want to track their attendance."
            ),
        )


async def _admin_cancel_clock_in(
    db: AsyncSession,
    device: AttendanceDevice,
    user_id: uuid.UUID,
    manager: User,
    reason: str,
) -> dict:
    """clock_in 을 취소하고 attendance 를 upcoming 으로 되돌림.

    on_break 인 경우 진행 중 break 도 cleanup. clock_out 이 이미 있는 row 는 거부.
    Manager 의 변경 사항은 attendance_corrections 에 기록 (note 컬럼은 직원 특이사항 전용).
    reason 은 클라이언트가 작성한 사유 그대로 사용.
    """
    from datetime import datetime as _dt, timezone as _tz
    from app.models.attendance import Attendance, AttendanceCorrection
    from app.models.attendance_break import AttendanceBreak
    from app.repositories.attendance_repository import attendance_repository
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))
    day_rows = await attendance_repository.list_user_day(db, user_id, today)
    target: Attendance | None = None
    for r in day_rows:
        if r.store_id == device.store_id and r.clock_in is not None and r.clock_out is None:
            target = r
            break
    if target is None:
        raise HTTPException(status_code=400, detail="No active clock-in to cancel")

    original_clock_in = target.clock_in.isoformat() if target.clock_in else None

    # 진행 중 break 종료(삭제) — clock_in 시점으로 ended_at 채워서 정리
    br_rows = (await db.execute(
        select(AttendanceBreak).where(AttendanceBreak.attendance_id == target.id)
    )).scalars().all()
    for br in br_rows:
        await db.delete(br)

    target.clock_in = None
    target.clock_in_timezone = None
    target.break_start = None
    target.break_end = None
    target.total_work_minutes = None
    target.total_break_minutes = None
    target.status = "upcoming"

    # 매니저 override → "modify" 태그. 단일 row 로 기록.
    # status 가 main 변경, clock_in 시각 정보는 reason 에 부속.
    user_reason = reason if reason and reason != "(no reason provided)" else None
    composed_reason = (
        f"Undo clock-in (clock-in was {original_clock_in})"
        if not user_reason
        else f"{user_reason} · clock-in was {original_clock_in}"
    )
    db.add(AttendanceCorrection(
        attendance_id=target.id,
        field_name="modify",
        original_value="working",
        corrected_value="upcoming",
        reason=composed_reason,
        corrected_by=manager.id,
    ))
    await db.flush()
    response = await attendance_service.build_response(db, target)
    await db.commit()
    return response


async def _admin_cancel_clock_out(
    db: AsyncSession,
    device: AttendanceDevice,
    user_id: uuid.UUID,
    manager: User,
    reason: str,
) -> dict:
    """clock_out 을 되돌림 — attendance 를 다시 working 상태로 복귀.

    clock_in 은 유지. clock_out / clock_out_timezone / total_work_minutes 만 초기화.
    안전: 오늘 + 이 매장의 clocked_out row 만 대상.
    clock_in 이 없는 row 를 reopen 하는 건 무의미하므로 거부 — clock-in 부터 다시 하라고 안내.
    reason 은 클라이언트가 작성한 사유 그대로 attendance_corrections 에 기록.
    """
    from datetime import datetime as _dt, timezone as _tz
    from app.models.attendance import Attendance, AttendanceCorrection
    from app.repositories.attendance_repository import attendance_repository
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))
    day_rows = await attendance_repository.list_user_day(db, user_id, today)
    target: Attendance | None = None
    for r in day_rows:
        if (
            r.store_id == device.store_id
            and r.clock_out is not None
            and r.status == "clocked_out"
        ):
            target = r
            break
    if target is None:
        raise HTTPException(
            status_code=400, detail="No completed shift to reopen"
        )
    # 정합성 — clock_in 없는 row 는 reopen 의미 없음. 사용자에게 Clock In 액션을 안내.
    if target.clock_in is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot reopen a shift without a clock-in time. "
                "Use the Clock In action to set a start time first."
            ),
        )

    original_clock_out = target.clock_out.isoformat() if target.clock_out else None

    target.clock_out = None
    target.clock_out_timezone = None
    target.total_work_minutes = None
    target.status = "working"
    # anomaly cleanup — early_clock_out 흔적 제거
    anoms = [a for a in (target.anomalies or []) if a != "early_clock_out"]
    target.anomalies = anoms or None

    # 매니저 override (Undo Clock-out) → "modify" 태그. 단일 row 로 기록.
    # status 가 main 변경, clock_out 시각 정보는 reason 에 부속.
    user_reason = reason if reason and reason != "(no reason provided)" else None
    composed_reason = (
        f"Undo clock-out (clock-out was {original_clock_out})"
        if not user_reason
        else f"{user_reason} · clock-out was {original_clock_out}"
    )
    db.add(AttendanceCorrection(
        attendance_id=target.id,
        field_name="modify",
        original_value="clocked_out",
        corrected_value="working",
        reason=composed_reason,
        corrected_by=manager.id,
    ))
    await db.flush()
    response = await attendance_service.build_response(db, target)
    await db.commit()
    return response


@router.get("/app-version", response_model=AppVersionResponse)
async def get_app_version(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppVersionResponse:
    """현재 환경 attendance 채널의 최신/최소 버전 + 다운로드 URL.

    Sideload APK 배포에서 클라이언트가 강제 업데이트 여부를 판단할 때 사용.
    등록 릴리스가 없으면 모든 필드 None → 클라이언트는 enforcement 없음으로 해석.
    """
    channel = app_version_service.attendance_channel()
    latest, min_version = await app_version_service.get_for_channel(db, channel)
    if latest is None:
        return AppVersionResponse()
    return AppVersionResponse(
        min_version=min_version,
        latest_version=latest.version,
        download_url=app_version_service.presigned_download_url(latest.s3_key),
        release_notes=latest.release_notes,
    )
