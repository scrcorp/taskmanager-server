"""Attendance kiosk 관리자 모드 라우터.

매장 SV/GM/Owner 가 키오스크 설정에서 PIN 인증 후 사용. manage token 은 in-memory.
별도 라우터로 분리하지 않고 같은 prefix /attendance 아래 /admin/* 로 묶음.

`/api/v1/attendance` 하위에 mount.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_attendance_manage_session,
    get_current_attendance_device,
)
from app.core.attendance_manage_session import (
    create_session as create_manage_session,
    revoke_session as revoke_manage_session,
)
from app.core.permissions import is_owner, is_sv_plus
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.models.organization import Store
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.schemas.attendance_device import (
    ManageAssignableUser,
    AdminClockActionRequest,
    ManageBreakEntry,
    ManageScheduleCreateRequest,
    ManageScheduleRow,
    ManageScheduleUpdateRequest,
    ManageSessionRequest,
    ManageSessionResponse,
    ManageStaffPinRevealResponse,
    ManageStaffPinRow,
    ManageStaffPinUpdateRequest,
    ManageStoreSettings,
    AdminStatusChangeRequest,
    ManageWorkRoleOption,
)
from app.schemas.schedule import KIOSK_STEP_MINUTES
from app.services.attendance_device_service import attendance_device_service
from app.services.attendance_service import attendance_service, compute_state_and_anomalies
from app.services.store_setting_service import upsert_store_setting
from app.utils.settings_resolver import (
    TIP_ENTRY_ENABLED_KEY,
    SettingNotRegisteredError,
    resolve_setting,
)


router: APIRouter = APIRouter()


@router.post("/manage/session", response_model=ManageSessionResponse, status_code=201)
async def manage_open_session(
    data: ManageSessionRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageSessionResponse:
    """PIN 으로 user 식별 + 매니저 자격 검증 후 manage session token 발급.

    user_id 입력 없이 PIN 하나로 user 식별 (organization 안에서 clockin_pin unique).
    """
    if device.store_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Device has no store assigned")

    # PIN 으로 user 식별 (organization 단위)
    manager = await attendance_device_service.identify_manager_by_pin(
        db, device.organization_id, data.pin
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

    session = create_manage_session(
        device_id=device.id,
        manager_user_id=manager.id,
        organization_id=device.organization_id,
        store_id=device.store_id,
    )
    # PIN 메뉴 노출 여부 — manage 진입(SV+)과 별개로 GM+ 기본인 clockin_pin permission 검사.
    from app.api.deps import user_has_permissions

    can_read_pins = await user_has_permissions(db, manager, "clockin_pin:read")
    can_update_pins = await user_has_permissions(db, manager, "clockin_pin:update")
    # Store Settings 도 같은 방식 — manage 진입(SV+)과 별개로 console 과 동일한 문턱.
    can_manage_store_settings = await user_has_permissions(db, manager, "stores:update")

    return ManageSessionResponse(
        manage_token=session.token,
        manager_user_id=manager.id,
        manager_name=manager.full_name or manager.username,
        expires_at=session.expires_at,
        can_read_pins=can_read_pins,
        can_update_pins=can_update_pins,
        can_manage_store_settings=can_manage_store_settings,
    )


@router.delete("/manage/session", status_code=204)
async def manage_close_session(
    request: Request,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
) -> None:
    """현재 admin session 종료 — UI Logout 버튼."""
    token = request.headers.get("X-Manage-Session") or request.headers.get("x-manage-session")
    revoke_manage_session(token)


# ── Admin Schedule CRUD ───────────────────────────────────


def _format_time_hhmm(t) -> str | None:
    if t is None:
        return None
    return t.strftime("%H:%M")


def _format_at(dt) -> str | None:
    """naive datetime → 벽시계 ISO "YYYY-MM-DDTHH:MM" (전환기 datetime 인코딩)."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")


def _parse_time_hhmm(s: str):
    from datetime import time as _time
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


def _kiosk_shift_iso(today, day_start, start_hhmm: str, end_hhmm: str) -> tuple[str, str]:
    """키오스크 HHmm 입력 → 영업일 창 기준 명시 벽시계 ISO(start_at, end_at).

    키오스크는 "오늘(영업일)" 스케줄만 다루고 날짜를 표현할 UI가 없다. 영업일 D의
    창은 [D의 경계, D+1의 경계)이므로, 경계(day_start, 기본 06:00) 이전 새벽 시각은
    달력상 D+1이다. 이 번역이 없으면 저녁에 만든 새벽조(01:00~05:00)가 D 01:00
    (이미 지난 시각)으로 앵커되어 즉시 no_show가 되는 실구멍이 있었다.
    """
    from datetime import datetime as _dt, timedelta as _td
    from app.utils.timezone import resolve_day_start_time

    t_start = _parse_time_hhmm(start_hhmm)
    t_end = _parse_time_hhmm(end_hhmm)
    next_day = today + _td(days=1)
    boundary_next = resolve_day_start_time(day_start, next_day.weekday())
    start_day = next_day if t_start < boundary_next else today
    end_day = start_day + _td(days=1) if t_end <= t_start else start_day
    return (
        _dt.combine(start_day, t_start).strftime("%Y-%m-%dT%H:%M"),
        _dt.combine(end_day, t_end).strftime("%Y-%m-%dT%H:%M"),
    )


async def _resolve_late_buffer(db: AsyncSession, organization_id, store_id) -> int:
    """attendance.late_buffer_minutes 설정 (없으면 5분)."""
    if organization_id is None:
        return 5
    try:
        raw = await resolve_setting(
            db,
            key="attendance.late_buffer_minutes",
            organization_id=organization_id,
            store_id=store_id,
        )
        return int(raw) if raw is not None else 5
    except (SettingNotRegisteredError, TypeError, ValueError):
        return 5


def _break_entries(breaks, tz_info) -> list[ManageBreakEntry]:
    """AttendanceBreak 목록 → ManageBreakEntry (store tz HH:mm, type normalize)."""
    from app.models.attendance_break import normalize_break_type

    def _hhmm(value):
        if value is None:
            return None
        try:
            return value.astimezone(tz_info).strftime("%H:%M")
        except Exception:
            return None

    out: list[ManageBreakEntry] = []
    for b in breaks:
        start = _hhmm(b.started_at)
        if start is None:
            continue
        out.append(
            ManageBreakEntry(type=normalize_break_type(b.break_type), start=start, end=_hhmm(b.ended_at))
        )
    return out


@router.get("/manage/schedules", response_model=list[ManageScheduleRow])
async def manage_list_today_schedules(
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ManageScheduleRow]:
    """현재 디바이스 매장의 오늘 스케줄 (status != cancelled/rejected/deleted)."""
    device, _session, _manager = auth
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo
    from app.models.attendance import Attendance
    from app.models.attendance_break import AttendanceBreak
    from app.models.schedule import Schedule, StoreWorkRole
    from app.models.work import Shift
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    now_utc = _dt.now(_tz.utc)
    today = get_work_date(store_tz, day_start, now_utc)
    tz_info = ZoneInfo(store_tz)

    rows = await db.execute(
        select(Schedule, User, Attendance, Shift.name)
        .join(User, User.id == Schedule.user_id)
        .outerjoin(Attendance, Attendance.schedule_id == Schedule.id)
        .outerjoin(StoreWorkRole, StoreWorkRole.id == Schedule.work_role_id)
        .outerjoin(Shift, Shift.id == StoreWorkRole.shift_id)
        .where(
            Schedule.store_id == device.store_id,
            Schedule.operating_day == today,
            Schedule.status.in_(("draft", "requested", "confirmed")),
        )
        .order_by(Schedule.start_at.asc().nulls_last(), User.full_name.asc())
    )
    all_rows = rows.all()

    def _display_tz(value):
        if value is None:
            return None
        try:
            return value.astimezone(tz_info).strftime("%H:%M")
        except Exception:
            return None

    # late_buffer (state/anomaly 시각 계산용)
    org_id = all_rows[0][0].organization_id if all_rows else None
    late_buffer = await _resolve_late_buffer(db, org_id, device.store_id)

    # breaks 일괄 조회 (attendance_id 별)
    att_ids = [att.id for _s, _u, att, _sh in all_rows if att is not None]
    breaks_by_att: dict = {}
    if att_ids:
        br_rows = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id.in_(att_ids))
            .order_by(AttendanceBreak.started_at.asc())
        )
        for b in br_rows.scalars().all():
            breaks_by_att.setdefault(b.attendance_id, []).append(b)

    result: list[ManageScheduleRow] = []
    for sched, user, att, shift_name in all_rows:
        state, anomalies = compute_state_and_anomalies(
            att_status=att.status if att else None,
            att_clock_in=att.clock_in if att else None,
            att_clock_out=att.clock_out if att else None,
            att_anomalies=att.anomalies if att else None,
            schedule_start_time=sched.start_time,
            schedule_end_time=sched.end_time,
            schedule_work_date=sched.work_date,
            now=now_utc,
            store_tz=tz_info,
            late_buffer=late_buffer,
            schedule_start_at=sched.start_at,
            schedule_end_at=sched.end_at,
        )
        breaks = _break_entries(breaks_by_att.get(att.id, []), tz_info) if att else []
        result.append(
            ManageScheduleRow(
                schedule_id=sched.id,
                user_id=user.id,
                user_name=user.full_name or user.username,
                work_role_id=sched.work_role_id,
                work_role_name=sched.work_role_name_snapshot,
                shift_name=shift_name,
                position_name=sched.position_snapshot,
                start_time=_format_time_hhmm(sched.start_time),
                end_time=_format_time_hhmm(sched.end_time),
                operating_day=sched.operating_day or sched.work_date,
                start_at=_format_at(sched.start_at),
                end_at=_format_at(sched.end_at),
                status=sched.status,
                attendance_id=att.id if att else None,
                state=state,
                anomalies=anomalies,
                breaks=breaks,
                attendance_status=att.status if att else None,
                clock_in_display=_display_tz(att.clock_in) if att else None,
                clock_out_display=_display_tz(att.clock_out) if att else None,
            )
        )
    return result


@router.get("/manage/assignable-users", response_model=list[ManageAssignableUser])
async def manage_list_assignable_users(
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ManageAssignableUser]:
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
        ManageAssignableUser(
            user_id=u.id,
            full_name=u.full_name or u.username,
            role_name=r.name,
        )
        for u, r in rows.all()
    ]


@router.get("/manage/work-roles", response_model=list[ManageWorkRoleOption])
async def manage_list_work_roles(
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ManageWorkRoleOption]:
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
        ManageWorkRoleOption(
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

    Kiosk manage 은 매니저가 직접 매장에서 즉시 운영을 하는 컨텍스트라 항상 confirmed.
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


@router.post("/manage/schedules", response_model=ManageScheduleRow, status_code=201)
async def manage_create_schedule(
    data: ManageScheduleCreateRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageScheduleRow:
    """오늘 새 스케줄을 매장에 생성. 항상 confirmed."""
    device, _session, manager = auth
    from datetime import datetime as _dt, timezone as _tz
    from app.schemas.schedule import ScheduleCreate
    from app.services.schedule_service import schedule_service
    from app.utils.timezone import get_store_day_config, get_work_date

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt.now(_tz.utc))

    # 키오스크 HHmm → 영업일 창 기준 명시 datetime (클라가 명시 start_at을 보내면 그것 우선)
    if data.start_at is None and data.start_time and data.end_time:
        _s_iso, _e_iso = _kiosk_shift_iso(today, day_start, data.start_time, data.end_time)
    else:
        _s_iso, _e_iso = data.start_at, data.end_at
    payload = ScheduleCreate(
        store_id=str(device.store_id),
        user_id=str(data.user_id),
        work_role_id=str(data.work_role_id) if data.work_role_id else None,
        work_date=today,
        start_time=data.start_time,
        end_time=data.end_time,
        operating_day=data.operating_day or today,
        start_at=_s_iso,
        end_at=_e_iso,
        status="confirmed",
        force=True,
    )
    response = await schedule_service.create_entry(
        db, device.organization_id, payload, created_by=manager.id,
        step_minutes=KIOSK_STEP_MINUTES,
    )
    # SV 매니저 권한이면 requested 로 떨어졌을 수 있음 → 강제 confirmed
    await _ensure_confirmed_today(db, uuid.UUID(response.id), device.organization_id, manager.id)
    # 재조회하여 응답 빌드
    return await _manage_schedule_row(db, uuid.UUID(response.id))


@router.patch("/manage/schedules/{schedule_id}", response_model=ManageScheduleRow)
async def manage_update_schedule(
    schedule_id: uuid.UUID,
    data: ManageScheduleUpdateRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageScheduleRow:
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

    # 키오스크 HHmm → 영업일 창 기준 명시 datetime (둘 다 온 경우만 번역, 명시 start_at 우선)
    if data.start_at is None and data.start_time and data.end_time:
        _s_iso, _e_iso = _kiosk_shift_iso(today, day_start, data.start_time, data.end_time)
    else:
        _s_iso, _e_iso = data.start_at, data.end_at
    payload = ScheduleUpdate(
        user_id=str(data.user_id) if data.user_id else None,
        work_role_id=str(data.work_role_id) if data.work_role_id else None,
        start_time=data.start_time,
        end_time=data.end_time,
        operating_day=data.operating_day,
        start_at=_s_iso,
        end_at=_e_iso,
        force=True,
        reset_checklist=True,
    )
    await schedule_service.update_entry(
        db, schedule_id, device.organization_id, payload, actor=manager,
        step_minutes=KIOSK_STEP_MINUTES,
    )
    return await _manage_schedule_row(db, schedule_id)


@router.delete("/manage/schedules/{schedule_id}", status_code=204)
async def manage_delete_schedule(
    schedule_id: uuid.UUID,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
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


async def _manage_schedule_row(db: AsyncSession, schedule_id: uuid.UUID) -> ManageScheduleRow:
    """단일 schedule_id → ManageScheduleRow 빌드."""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo
    from app.models.attendance import Attendance
    from app.models.attendance_break import AttendanceBreak
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
    now_utc = _dt.now(_tz.utc)
    late_buffer = await _resolve_late_buffer(db, sched.organization_id, sched.store_id)

    def _display_tz(value):
        if value is None:
            return None
        try:
            return value.astimezone(tz_info).strftime("%H:%M")
        except Exception:
            return None

    state, anomalies = compute_state_and_anomalies(
        att_status=att.status if att else None,
        att_clock_in=att.clock_in if att else None,
        att_clock_out=att.clock_out if att else None,
        att_anomalies=att.anomalies if att else None,
        schedule_start_time=sched.start_time,
        schedule_end_time=sched.end_time,
        schedule_work_date=sched.work_date,
        now=now_utc,
        store_tz=tz_info,
        late_buffer=late_buffer,
        schedule_start_at=sched.start_at,
        schedule_end_at=sched.end_at,
    )

    breaks: list[ManageBreakEntry] = []
    if att is not None:
        br_rows = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id == att.id)
            .order_by(AttendanceBreak.started_at.asc())
        )
        breaks = _break_entries(br_rows.scalars().all(), tz_info)

    return ManageScheduleRow(
        schedule_id=sched.id,
        user_id=user.id,
        user_name=user.full_name or user.username,
        work_role_id=sched.work_role_id,
        work_role_name=sched.work_role_name_snapshot,
        shift_name=shift_name,
        position_name=sched.position_snapshot,
        start_time=_format_time_hhmm(sched.start_time),
        end_time=_format_time_hhmm(sched.end_time),
        operating_day=sched.operating_day or sched.work_date,
        start_at=_format_at(sched.start_at),
        end_at=_format_at(sched.end_at),
        status=sched.status,
        attendance_id=att.id if att else None,
        state=state,
        anomalies=anomalies,
        breaks=breaks,
        attendance_status=att.status if att else None,
        clock_in_display=_display_tz(att.clock_in) if att else None,
        clock_out_display=_display_tz(att.clock_out) if att else None,
    )


# ── Admin Attendance Override ─────────────────────────────


@router.post("/manage/clock")
async def manage_clock_action(
    data: AdminClockActionRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
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
        return await _manage_cancel_clock_in(db, device, data.user_id, manager, reason)
    if action == "cancel_clock_out":
        return await _manage_cancel_clock_out(db, device, data.user_id, manager, reason)

    valid_actions = {"clock_in", "clock_out", "break_start", "break_end"}
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # service 가 모든 attendance 액션을 attendance_corrections 에 자동 기록.
    # reason 은 service 가 actor 라벨로 생성하지만, 매니저가 textfield 에 입력했으면
    # 그 값을 reason 으로 전달해 service 가 우선 사용.
    attendance = await attendance_device_service.perform_clock_action_manage(
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


@router.post("/manage/attendance/status")
async def manage_change_attendance_status(
    data: AdminStatusChangeRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
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
        """영업일(today) 기준 시각 → 실제 달력일 instant.

        영업일 D의 창은 [D의 경계, D+1의 경계). 경계(day_start, 기본 06:00) 이전 새벽
        시각은 달력상 D+1에 속한다 — 마감조 clock_out 02:00, 새벽 워크인 clock_in 01:30 등을
        영업일 달력일(D)로 합성하면 하루 어긋나던 버그 수정.
        """
        from datetime import timedelta as _td
        from app.utils.timezone import resolve_day_start_time
        hh, mm = hhmm.split(":")
        t = _t(int(hh), int(mm))
        next_day = today + _td(days=1)
        boundary_next = resolve_day_start_time(day_start, next_day.weekday())
        d = next_day if t < boundary_next else today
        return _dt.combine(d, t, tzinfo=tz_info)

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
            Schedule.operating_day == today,
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


async def _manage_cancel_clock_in(
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


async def _manage_cancel_clock_out(
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



# ── 직원 PIN 관리 (Staff PINs) ─────────────────────────────
#
# manage 세션 진입 문턱은 SV+ 인데 PIN 문턱은 GM+ 기본(`clockin_pin:*`).
# 세션 토큰만으로 열면 SV 가 부하 직원 PIN 을 전부 보게 되므로, 매 요청마다
# 세션의 manager 로 permission 을 다시 검사한다. 대상 직원은 반드시
# **이 기기의 매장에 배정된 사람**이어야 한다 — 이 스코프 검사가 빠지면
# org 전체 PIN 이 키오스크에서 열린다.


async def _require_manager_permission(
    db: AsyncSession, manager: User, *codes: str
) -> None:
    """manage 세션 매니저가 해당 permission 을 가졌는지 검사.

    manage 세션 자체는 SV+ 문턱이라, 그보다 높은 기능(PIN, 매장 설정)은 여기서
    console 과 같은 permission 을 다시 요구한다.
    """
    from app.api.deps import user_has_permissions

    if not await user_has_permissions(db, manager, *codes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission required: {codes[0]}",
        )


async def _require_pin_permission(
    db: AsyncSession, manager: User, *codes: str
) -> None:
    """manage 세션 매니저가 PIN permission 을 가졌는지 검사."""
    await _require_manager_permission(db, manager, *codes)


async def _load_store_staff(
    db: AsyncSession, device: AttendanceDevice, user_id: uuid.UUID
) -> User:
    """이 기기 매장에 배정된 직원 1명 로드. 아니면 404 (존재 여부 노출 안 함)."""
    result = await db.execute(
        select(User)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            User.id == user_id,
            User.organization_id == device.organization_id,
            UserStore.store_id == device.store_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
        .limit(1)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found in this store")
    return user


@router.get("/manage/staff-pins", response_model=list[ManageStaffPinRow])
async def manage_list_staff_pins(
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
) -> list[ManageStaffPinRow]:
    """이 매장 직원 목록 — 오늘 근무자 우선, 그다음 이름순.

    평문 PIN 은 담지 않는다(`has_pin` 만). 평문은 reveal 엔드포인트로만 나가고
    그때 감사 로그가 남는다.
    """
    from datetime import datetime as _dt2, timezone as _tz2
    from app.models.schedule import Schedule
    from app.utils.timezone import get_store_day_config, get_work_date

    device, _session, manager = auth
    await _require_pin_permission(db, manager, "clockin_pin:read")

    store_tz, day_start = await get_store_day_config(db, device.store_id)
    today = get_work_date(store_tz, day_start, _dt2.now(_tz2.utc))

    # 오늘 이 매장에 살아있는 스케줄이 있는 user 집합 — 정렬 키로만 쓴다(필터 아님).
    today_rows = await db.execute(
        select(Schedule.user_id).where(
            Schedule.store_id == device.store_id,
            Schedule.operating_day == today,
            Schedule.status.in_(("draft", "requested", "confirmed")),
        )
    )
    works_today_ids = {r for (r,) in today_rows.all()}

    stmt = (
        select(User, Role)
        .join(Role, User.role_id == Role.id)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            UserStore.store_id == device.store_id,
            User.organization_id == device.organization_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(term),
                User.username.ilike(term),
                User.employee_no.ilike(term),
            )
        )
    rows = (await db.execute(stmt)).all()

    items = [
        ManageStaffPinRow(
            user_id=u.id,
            full_name=u.full_name or u.username,
            employee_no=u.employee_no,
            role_name=r.name if r else None,
            has_pin=u.clockin_pin is not None,
            works_today=u.id in works_today_ids,
        )
        for u, r in rows
    ]
    # 오늘 근무자 우선 → 이름순
    items.sort(key=lambda i: (not i.works_today, i.full_name.lower()))
    return items


@router.get(
    "/manage/staff-pins/{user_id}/reveal",
    response_model=ManageStaffPinRevealResponse,
)
async def manage_reveal_staff_pin(
    user_id: uuid.UUID,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageStaffPinRevealResponse:
    """평문 PIN 1건 + 감사 기록. 목록이 아니라 여기서만 평문이 나간다."""
    from app.models.clockin_pin_audit import PIN_AUDIT_REVEAL
    from app.repositories.clockin_pin_audit_repository import (
        clockin_pin_audit_repository,
    )

    device, session_obj, manager = auth
    await _require_pin_permission(db, manager, "clockin_pin:read")
    target = await _load_store_staff(db, device, user_id)

    await clockin_pin_audit_repository.record(
        db,
        organization_id=device.organization_id,
        actor_user_id=manager.id,
        target_user_id=target.id,
        action=PIN_AUDIT_REVEAL,
        device_id=device.id,
        store_id=session_obj.store_id,
        meta={"source": "kiosk_manage"},
    )
    await db.commit()
    return ManageStaffPinRevealResponse(
        user_id=target.id, clockin_pin=target.clockin_pin
    )


@router.patch(
    "/manage/staff-pins/{user_id}", response_model=ManageStaffPinRevealResponse
)
async def manage_update_staff_pin(
    user_id: uuid.UUID,
    data: ManageStaffPinUpdateRequest,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageStaffPinRevealResponse:
    """직원 PIN 을 직접 지정 (4~6자리). 중복·prefix 충돌 시 409."""
    from app.models.clockin_pin_audit import PIN_AUDIT_UPDATE
    from app.repositories.clockin_pin_audit_repository import (
        clockin_pin_audit_repository,
    )
    from app.services.attendance_device_service import (
        assert_no_pin_prefix_conflict,
        commit_pin_or_409,
    )

    device, session_obj, manager = auth
    await _require_pin_permission(db, manager, "clockin_pin:update")
    target = await _load_store_staff(db, device, user_id)

    # store_id 전달 — 충돌 유저가 이 기기 매장 밖이면 409 detail.other_store=true
    await assert_no_pin_prefix_conflict(
        db,
        device.organization_id,
        data.clockin_pin,
        exclude_user_id=target.id,
        store_id=device.store_id,
    )
    target.clockin_pin = data.clockin_pin
    await clockin_pin_audit_repository.record(
        db,
        organization_id=device.organization_id,
        actor_user_id=manager.id,
        target_user_id=target.id,
        action=PIN_AUDIT_UPDATE,
        device_id=device.id,
        store_id=session_obj.store_id,
        meta={"pin_length": len(data.clockin_pin), "source": "kiosk_manage"},
    )
    await commit_pin_or_409(db)
    return ManageStaffPinRevealResponse(
        user_id=target.id, clockin_pin=target.clockin_pin
    )


@router.post(
    "/manage/staff-pins/{user_id}/regenerate",
    response_model=ManageStaffPinRevealResponse,
)
async def manage_regenerate_staff_pin(
    user_id: uuid.UUID,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageStaffPinRevealResponse:
    """직원 PIN 랜덤 재발급 (6자리). 새 PIN 을 그대로 반환해 바로 안내 가능."""
    from app.models.clockin_pin_audit import PIN_AUDIT_REGENERATE
    from app.repositories.clockin_pin_audit_repository import (
        clockin_pin_audit_repository,
    )
    from app.services.attendance_device_service import (
        commit_pin_or_409,
        generate_unique_clockin_pin,
    )

    device, session_obj, manager = auth
    await _require_pin_permission(db, manager, "clockin_pin:update")
    target = await _load_store_staff(db, device, user_id)

    target.clockin_pin = await generate_unique_clockin_pin(
        db, device.organization_id, exclude_user_id=target.id
    )
    await clockin_pin_audit_repository.record(
        db,
        organization_id=device.organization_id,
        actor_user_id=manager.id,
        target_user_id=target.id,
        action=PIN_AUDIT_REGENERATE,
        device_id=device.id,
        store_id=session_obj.store_id,
        meta={"pin_length": len(target.clockin_pin), "source": "kiosk_manage"},
    )
    await commit_pin_or_409(db)
    return ManageStaffPinRevealResponse(
        user_id=target.id, clockin_pin=target.clockin_pin
    )


# ── Kiosk 관리자 모드 — 매장 설정 ────────────────────────────

@router.get("/manage/store-settings", response_model=ManageStoreSettings)
async def manage_get_store_settings(
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageStoreSettings:
    """이 기기가 속한 매장의 설정 (resolve 값). 읽기는 manage 세션이면 허용."""
    device, _session, _manager = auth
    try:
        raw = await resolve_setting(
            db,
            key=TIP_ENTRY_ENABLED_KEY,
            organization_id=device.organization_id,
            store_id=device.store_id,
        )
        tip_entry_enabled = bool(raw)
    except SettingNotRegisteredError:
        tip_entry_enabled = False
    return ManageStoreSettings(tip_entry_enabled=tip_entry_enabled)


@router.put("/manage/store-settings", response_model=ManageStoreSettings)
async def manage_update_store_settings(
    data: ManageStoreSettings,
    auth: Annotated[tuple, Depends(get_current_attendance_manage_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageStoreSettings:
    """매장 설정 변경 — console 과 같은 StoreSetting row 를 쓴다.

    manage 진입 문턱(SV+)보다 높은 stores:update 를 요구한다 (console 과 동일).
    같은 매장의 다른 기기는 다음 device 폴링에서 새 값을 받는다.
    """
    device, _session, manager = auth
    await _require_manager_permission(db, manager, "stores:update")
    await upsert_store_setting(
        db,
        store_id=device.store_id,
        organization_id=device.organization_id,
        key=TIP_ENTRY_ENABLED_KEY,
        value=data.tip_entry_enabled,
        updated_by=manager.id,
    )
    await db.commit()
    return ManageStoreSettings(tip_entry_enabled=data.tip_entry_enabled)
