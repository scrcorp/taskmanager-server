"""Attendance device 대시보드 데이터 라우터 — today-staff / notices.

`/api/v1/attendance` 하위에 mount.
"""

import hashlib
import json
import uuid
from datetime import date as date_cls, datetime, datetime as dt, timedelta, timezone as tz
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response, status as http_status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.database import get_db
from app.models.attendance import Attendance
from app.models.attendance_break import (
    PAID_BREAK_TYPES,
    UNPAID_BREAK_TYPES,
    AttendanceBreak,
)
from app.models.attendance_device import AttendanceDevice
from app.models.communication import Notice
from app.models.schedule import Schedule
from app.models.user import User
from app.models.attendance_break import normalize_break_type
from app.schemas.attendance_device import (
    ManageBreakEntry,
    NoticeRow,
    TodayStaffBreak,
    TodayStaffRow,
)
from app.services.attendance_service import compute_effective_status, compute_state_and_anomalies
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
from app.utils.timezone import get_store_day_config, get_work_date, resolve_schedule_instants


router: APIRouter = APIRouter()


@router.get("/today-staff", response_model=list[TodayStaffRow])
async def today_staff(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    response: Response,
) -> list[TodayStaffRow] | Response:
    """기기 매장 기준 오늘 스케줄 + 각 유저의 현재 attendance 상태.

    한 번의 호출로 On Shift / Coming Up / Completed 를 모두 반환. 클라이언트가
    status 로 분기해서 섹션에 배치.
    """
    if device.store_id is None:
        return []

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

    def display_store_tz(value):
        if value is None:
            return None
        return value.astimezone(tz_info).strftime("%H:%M")

    def effective_status(att: Attendance, schedule: Schedule | None) -> str:
        """compute_effective_status (attendance_service) 의 thin wrapper.

        Issue 3 (2026-05-28): 기존엔 inline 으로 같은 로직을 또 구현했는데,
        compute_effective_status 와 'late' 처리가 drift 함 (저쪽은 clock_in 있으면
        'late' → 'working' 으로 승격, 여기는 DB status 그대로 'late'). 단일 출처로 통일.
        """
        return compute_effective_status(
            att_status=att.status,
            att_clock_in=att.clock_in,
            schedule_start_time=schedule.start_time if schedule else None,
            schedule_end_time=schedule.end_time if schedule else None,
            schedule_work_date=schedule.work_date if schedule else None,
            now=now,
            store_tz=tz_info,
            late_buffer=late_buffer,
            soon_threshold_minutes=SOON_THRESHOLD_MINUTES,
            schedule_start_at=schedule.start_at if schedule else None,
            schedule_end_at=schedule.end_at if schedule else None,
        )

    result: list[TodayStaffRow] = []
    for att, schedule, user in triples:
        paid = unpaid = 0
        current: TodayStaffBreak | None = None
        break_entries: list[ManageBreakEntry] = []
        for br in sorted(break_map.get(att.id, []), key=lambda b: b.started_at):
            start_disp = display_store_tz(br.started_at)
            if start_disp is not None:
                break_entries.append(ManageBreakEntry(
                    type=normalize_break_type(br.break_type),
                    start=start_disp,
                    end=display_store_tz(br.ended_at),
                ))
            if br.ended_at is None:
                current = TodayStaffBreak(
                    started_at=br.started_at, break_type=br.break_type
                )
            else:
                if br.break_type in PAID_BREAK_TYPES:
                    paid += br.duration_minutes or 0
                elif br.break_type in UNPAID_BREAK_TYPES:
                    unpaid += br.duration_minutes or 0

        state, anomalies = compute_state_and_anomalies(
            att_status=att.status,
            att_clock_in=att.clock_in,
            att_clock_out=att.clock_out,
            att_anomalies=att.anomalies,
            schedule_start_time=schedule.start_time if schedule else None,
            schedule_end_time=schedule.end_time if schedule else None,
            schedule_work_date=schedule.work_date if schedule else None,
            now=now,
            store_tz=tz_info,
            late_buffer=late_buffer,
            schedule_start_at=schedule.start_at if schedule else None,
            schedule_end_at=schedule.end_at if schedule else None,
        )

        # start_at 우선, 없으면 combine 폴백 (overnight 보정 포함)
        if schedule is not None:
            sched_start, sched_end = resolve_schedule_instants(
                start_at=schedule.start_at, end_at=schedule.end_at,
                work_date=schedule.work_date, start_time=schedule.start_time,
                end_time=schedule.end_time, tz_name=tz_info.key,
            )
        else:
            sched_start = sched_end = None
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
                state=state,
                anomalies=anomalies,
                breaks=break_entries,
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

    # ── ETag/304: 응답 내용 해시가 같으면 304(빈 바디)로 전송량 절감.
    #    status/state 는 시간 파생이라 "실제 표시가 바뀔 때만" 해시가 달라진다
    #    (아무 변화 없는 폴링 tick → 304). 304 는 표준이라 프록시 통과 문제 없음.
    payload = [r.model_dump(mode="json") for r in result]
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    etag = 'W/"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=http_status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@router.get("/notices", response_model=list[NoticeRow])
async def notices(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 10,
) -> list[NoticeRow]:
    """기기 store 대상 공지 (최근 N개, 기본 10)."""
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
