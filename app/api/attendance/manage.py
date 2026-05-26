"""Attendance kiosk 관리자 모드 라우터.

매장 SV/GM/Owner 가 키오스크 설정에서 PIN 인증 후 사용. manage token 은 in-memory.
별도 라우터로 분리하지 않고 같은 prefix /attendance 아래 /admin/* 로 묶음.

`/api/v1/attendance` 하위에 mount.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
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
    ManageScheduleCreateRequest,
    ManageScheduleRow,
    ManageScheduleUpdateRequest,
    ManageSessionRequest,
    ManageSessionResponse,
    AdminStatusChangeRequest,
    ManageWorkRoleOption,
)
from app.services.attendance_device_service import attendance_device_service


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
    return ManageSessionResponse(
        manage_token=session.token,
        manager_user_id=manager.id,
        manager_name=manager.full_name or manager.username,
        expires_at=session.expires_at,
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


def _parse_time_hhmm(s: str):
    from datetime import time as _time
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


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

    result: list[ManageScheduleRow] = []
    for sched, user, att, shift_name in rows.all():
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
                status=sched.status,
                attendance_id=att.id if att else None,
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
        status=sched.status,
        attendance_id=att.id if att else None,
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
        return await _admin_cancel_clock_in(db, device, data.user_id, manager, reason)
    if action == "cancel_clock_out":
        return await _admin_cancel_clock_out(db, device, data.user_id, manager, reason)

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


