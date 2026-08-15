"""스케줄 일일 보고서 — 이슈 detect + diff + 이메일.

이슈 종류:
    - shift_understaffed: 매장×시프트×날짜에 confirmed schedule 0건
    - sv_missing: 위 그룹에 SV (priority=30) 0명
    - over_6h: 유저×날짜 net_work_minutes 합계 > 360
    - no_break_8h: 유저×날짜 합계 ≥ 480 & 모든 schedule에 휴게 없음

이슈 key는 set diff 식별자 (대상날짜 포함). label 등 표시용 메타는 함께 저장.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permissions import SV_PRIORITY
from app.models.attendance import Attendance
from app.models.organization import Organization, ShiftPreset, Store
from app.models.schedule import Schedule, StoreWorkRole
from app.models.schedule_report import ScheduleReportSnapshot
from app.models.user import Role, User
from app.models.work import Shift
from app.seeds.settings_seed import (
    SCHEDULE_RANGE_KEY,
    SCHEDULE_REPORT_RECIPIENTS_KEY,
    SCHEDULE_REPORT_TIMES_KEY,
    STORE_OPERATING_HOURS_KEY,
)
from app.utils.email import send_email
from app.utils.email_templates import build_schedule_daily_report_email
from app.utils.schedule_report_pdf import build_schedule_daily_report_pdf
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
from app.utils.timezone import is_closed_weekday, resolve_day_range

logger = logging.getLogger("uvicorn.error")

CONFIRMED_STATUSES = ("confirmed",)
OVER_HOURS_MINUTES = 360  # 6h
NO_BREAK_MINUTES = 480  # 8h
LOOKAHEAD_DAYS = 3  # today + next 2 = 3 days

CATEGORY_LABELS = {
    "shift_understaffed": "Understaffed shift",
    "sv_missing": "No supervisor",
    "over_6h": "Over 6h work",
    "no_break_8h": "No break with 8h+",
}


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _minutes_from_operating_day(anchor: date, dt: datetime | None) -> int | None:
    """벽시계 datetime 을 영업일 라벨 기준 분(minute) 공간으로 편다.

    `operating_day` 는 영업일 라벨이고 `start_at`/`end_at` 은 각자 자기 날짜를 가진다
    (자정 넘김 근무면 라벨보다 하루 뒤). 두 값을 같은 공간에 올리는 유일하게 옳은 방법은
    **날짜 차이를 그대로 더하는 것**이다.

    `end <= start 이면 +1440` 같은 암묵 보정을 쓰면 안 된다 — CLAUDE.md 의
    Time Representation Policy 금지사항 4번이고, 실제로 sv_gap 을 깨뜨렸다.
    비교 대상인 `resolve_day_range` 의 종료분은 `end_offset_days` 가 반영돼
    정당하게 1440 을 넘는데, 보정으로 만든 값은 그 공간에 없기 때문이다.
    """
    if dt is None:
        return None
    return (dt.date() - anchor).days * 1440 + dt.hour * 60 + dt.minute


def _minutes_to_label(m: int) -> str:
    h = (m // 60) % 24
    mm = m % 60
    return f"{h:02d}:{mm:02d}"


def _merge_intervals(items: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not items:
        return []
    items = sorted(items, key=lambda x: x[0])
    out = [items[0]]
    for s, e in items[1:]:
        last_s, last_e = out[-1]
        if s <= last_e:
            out[-1] = (last_s, max(last_e, e))
        else:
            out.append((s, e))
    return out


def _subtract_intervals(base_s: int, base_e: int, cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[base_s, base_e] 에서 cuts 빼기 → 남은 구간들. cuts 는 disjoint."""
    cuts = _merge_intervals([(max(s, base_s), min(e, base_e)) for s, e in cuts if e > base_s and s < base_e])
    out: list[tuple[int, int]] = []
    cur = base_s
    for s, e in cuts:
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < base_e:
        out.append((cur, base_e))
    return out


async def _resolve_setting_value(
    db: AsyncSession,
    key: str,
    organization_id: UUID,
    store_id: UUID,
) -> dict | None:
    """매장 → org → registry default cascade 로 시간 범위 설정을 읽는다.

    키가 registry 에 없으면(구 DB, 마이그레이션 전) None — 호출부가 "미설정"으로 다룬다.
    """
    try:
        raw = await resolve_setting(db, key, organization_id, store_id)
    except SettingNotRegisteredError:
        return None
    return raw if isinstance(raw, dict) else None


async def _resolve_schedule_window(
    db: AsyncSession,
    organization_id: UUID,
    store_id: UUID,
    target_date: date,
) -> tuple[int, int] | None:
    """SV 공백 판정용 창 = **스케줄 시간대**(`schedule.range`) — 영업시간이 아니다 (D2-5).

    직원이 일하는 모든 시간에 SV가 있어야 한다는 규칙이므로 기준은 근무 가능 시간대다.
    영업시간(store.operating_hours)으로 바꾸면 프렙·마감 시간대가 검사에서 빠진다.
    """
    raw = await _resolve_setting_value(db, SCHEDULE_RANGE_KEY, organization_id, store_id)
    return resolve_day_range(raw, target_date.weekday())


def _shift_within_operating_hours(
    operating_hours: dict | None,
    target_date: date,
    shift_start: time | None,
    shift_end: time | None,
) -> bool:
    """시프트가 매장 영업시간과 겹치는지 — 인원 부족 검사 대상 판정.

    Returns True 이면 검사 대상. False 이면 영업시간 밖(또는 휴무) — 스킵.
    영업시간 미설정/시프트 시간 미설정 시 보수적으로 True (검사) —
    설정을 안 한 매장의 리포트가 조용히 비는 일은 없어야 한다.

    출처는 `store.operating_hours` **설정 키**다. 예전엔 `stores.operating_hours`
    컬럼을 봤는데 전 매장이 NULL 이라 사실상 죽은 검사였고, 컬럼은 미설정이 NULL 이라
    호출부마다 폴백을 각자 짜게 된다. registry 는 매장 → 조직 → 기본값 cascade 를 준다 (D2-3).
    """
    window = resolve_day_range(operating_hours, target_date.weekday())
    if window is None:
        # 휴무일이면 그 요일에 검사할 시프트가 없다. 미설정과 구분해야 한다 —
        # 미설정은 "전부 검사", 휴무는 "검사 없음"으로 정반대다.
        return not is_closed_weekday(operating_hours, target_date.weekday())
    if shift_start is None or shift_end is None:
        return True
    open_m, close_m = window
    start_m = _time_to_minutes(shift_start)
    end_m = _time_to_minutes(shift_end)
    if end_m <= start_m:
        end_m += 1440  # 시프트 프리셋은 아직 time 쌍이라 자정 넘김을 여기서 편다
    return start_m >= open_m and end_m <= close_m


@dataclass(frozen=True)
class StoreInfo:
    """모든 active 매장 — work_role/schedule 유무와 무관하게 메일 섹션 표시용."""

    id: str
    name: str


@dataclass(frozen=True)
class ShiftCell:
    """검사 대상 (store, shift, date) 셀 + 인원/SV 카운트.

    이메일 빌더에서 정상 셀도 인원수를 표시할 수 있게 detection 시점에 함께 계산.
    """

    store_id: str
    store_name: str
    shift_id: str
    shift_name: str
    shift_sort_order: int
    target_date: date
    staff_count: int
    sv_count: int


@dataclass(frozen=True)
class Issue:
    key: str
    category: str
    target_date: str  # ISO date
    label: str
    store_id: str | None
    store_name: str | None
    shift_id: str | None
    shift_name: str | None
    user_id: str | None
    user_name: str | None
    detail: dict

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, data: dict) -> "Issue":
        return cls(**data)


async def collect_cells_and_issues(
    db: AsyncSession,
    organization_id: UUID,
    target_dates: list[date],
) -> tuple[list[StoreInfo], list[ShiftCell], list[Issue]]:
    """org × target_dates 의 매장 목록 + 검사 셀 + 이슈.

    Returns:
        stores: 모든 active 매장 (cells/issues 없어도 메일 섹션 표시용)
        cells: 검사 대상 (store, shift, date) — 운영시간 내. 정상 셀도 포함.
        issues: 감지된 이슈 list
    """
    issues: list[Issue] = []
    cells: list[ShiftCell] = []

    stores = (
        await db.execute(
            select(Store).where(
                Store.organization_id == organization_id,
                Store.deleted_at.is_(None),
                Store.is_active.is_(True),
            ).order_by(Store.created_at)  # 콘솔 매장 목록과 동일 정렬
        )
    ).scalars().all()
    stores_info = [StoreInfo(id=str(s.id), name=s.name) for s in stores]
    if not stores:
        return stores_info, cells, issues
    store_map = {s.id: s for s in stores}
    store_ids = list(store_map.keys())

    shifts = (
        await db.execute(select(Shift).where(Shift.store_id.in_(store_ids)))
    ).scalars().all()
    shift_map = {s.id: s for s in shifts}

    work_roles = (
        await db.execute(
            select(StoreWorkRole).where(
                StoreWorkRole.store_id.in_(store_ids),
                StoreWorkRole.is_active.is_(True),
            )
        )
    ).scalars().all()
    shift_presets = (
        await db.execute(
            select(ShiftPreset).where(
                ShiftPreset.store_id.in_(store_ids),
                ShiftPreset.is_active.is_(True),
            )
        )
    ).scalars().all()

    # (store, shift) 시간 윈도우: shift_preset 우선 → work_role 보완
    # 둘 다 없으면 (None, None) — 운영시간 비교 못 함 → 검사 통과 (보수적).
    shift_window: dict[tuple[UUID, UUID], tuple[time | None, time | None]] = {}

    def _expand(key: tuple[UUID, UUID], s: time | None, e: time | None) -> None:
        cur_s, cur_e = shift_window.get(key, (None, None))
        if s is not None and (cur_s is None or s < cur_s):
            cur_s = s
        if e is not None and (cur_e is None or e > cur_e):
            cur_e = e
        shift_window[key] = (cur_s, cur_e)

    for sp in shift_presets:
        _expand((sp.store_id, sp.shift_id), sp.start_time, sp.end_time)
    for wr in work_roles:
        _expand((wr.store_id, wr.shift_id), wr.default_start_time, wr.default_end_time)

    # 영업시간은 설정 cascade 라 매장당 한 번만 해석한다 (요일별 값은 한 dict 안에 있음).
    operating_hours_by_store: dict[UUID, dict | None] = {
        sid: await _resolve_setting_value(db, STORE_OPERATING_HOURS_KEY, organization_id, sid)
        for sid in store_ids
    }

    # 매장이 정의한 모든 shifts 가 후보. work_role/schedule 유무와 무관.
    # → 같은 매장이라면 시프트 정의가 동일하게 표시되도록 일관성 보장.
    pending_cells: list[tuple[UUID, UUID, date, Store, Shift]] = []
    for shift in shifts:
        store = store_map.get(shift.store_id)
        if not store:
            continue
        s_start, s_end = shift_window.get((shift.store_id, shift.id), (None, None))
        operating_hours = operating_hours_by_store.get(shift.store_id)
        for d in target_dates:
            if not _shift_within_operating_hours(operating_hours, d, s_start, s_end):
                continue
            pending_cells.append((shift.store_id, shift.id, d, store, shift))

    rows = (
        await db.execute(
            select(Schedule, User, Role, StoreWorkRole)
            .outerjoin(User, Schedule.user_id == User.id)
            .outerjoin(Role, User.role_id == Role.id)
            .outerjoin(StoreWorkRole, Schedule.work_role_id == StoreWorkRole.id)
            .where(
                Schedule.organization_id == organization_id,
                Schedule.operating_day.in_(target_dates),
                Schedule.status.in_(CONFIRMED_STATUSES),
            )
        )
    ).all()

    # 1) shift_understaffed + sv_missing — (store, shift, date) 그룹
    by_shift: dict[tuple[UUID, UUID, date], list[tuple[Schedule, User | None, Role | None]]] = {}
    for sch, user, role, wr in rows:
        if sch.store_id is None or wr is None or wr.shift_id is None:
            continue
        by_shift.setdefault((sch.store_id, wr.shift_id, sch.work_date), []).append((sch, user, role))

    # work_role 이 없는 confirmed 스케줄은 위 버킷에서 통째로 빠진다 — 워크인 스케줄은
    # work_role 없이 생성되고(schedule_service 의 origin="walk_in"), work_role_id 는
    # ondelete="SET NULL" 이라 매니저가 work role 을 지우면 기존 스케줄이 여기로 떨어진다.
    # 빠졌다고 "0명"인 건 아니다: 같은 행이 아래 over_6h / sv_gap 에서는 실근무로 집계되므로,
    # 방치하면 한 사람이 같은 리포트에서 "0 staff scheduled" 이면서 "6시간 초과"로 동시에 잡힌다.
    roleless_by_store_date: dict[tuple[UUID, date], int] = {}
    for sch, _, _, wr in rows:
        if sch.store_id is None:
            continue
        if wr is not None and wr.shift_id is not None:
            continue
        k = (sch.store_id, sch.operating_day)
        roleless_by_store_date[k] = roleless_by_store_date.get(k, 0) + 1

    for store_uuid, shift_uuid, d, store, shift in pending_cells:
        d_iso = d.isoformat()
        members = by_shift.get((store_uuid, shift_uuid, d), [])
        sv_count = sum(1 for (_, _, r) in members if r and r.priority == SV_PRIORITY)
        cells.append(ShiftCell(
            store_id=str(store_uuid),
            store_name=store.name,
            shift_id=str(shift_uuid),
            shift_name=shift.name,
            shift_sort_order=shift.sort_order,
            target_date=d,
            staff_count=len(members),
            sv_count=sv_count,
        ))
        common_detail = {
            "shift_sort_order": shift.sort_order,
            "staff_count": len(members),
        }
        # 이 매장·날짜에 shift 로 귀속되지 않은 근무자가 있으면 "0명" 경고를 내지 않는다.
        # (매장 단위 억제 — 워크인은 어느 shift 소속인지 알 수 없으므로 shift 단위로 좁힐 수 없다)
        if not members and not roleless_by_store_date.get((store_uuid, d)):
            issues.append(Issue(
                key=f"shift_understaffed|{store_uuid}|{shift_uuid}|{d_iso}",
                category="shift_understaffed",
                target_date=d_iso,
                label=f"{store.name} – {shift.name}: 0 staff scheduled",
                store_id=str(store_uuid),
                store_name=store.name,
                shift_id=str(shift_uuid),
                shift_name=shift.name,
                user_id=None,
                user_name=None,
                detail=common_detail,
            ))
        # sv_missing (시프트 단위) 는 제거 — SV 부족은 sv_gap (시간 기반) 에서 매장×날짜 단위로 검출.

    # 2) over_6h + no_break_8h — (user, date) 그룹
    by_user: dict[tuple[UUID, date], list[Schedule]] = {}
    user_map: dict[UUID, User] = {}
    for sch, user, _, _ in rows:
        if user is None:
            continue
        user_map[user.id] = user
        by_user.setdefault((user.id, sch.work_date), []).append(sch)

    for (uid, d), schs in by_user.items():
        user = user_map[uid]
        total_min = sum(s.net_work_minutes for s in schs)
        has_break = any(s.break_start_time and s.break_end_time for s in schs)
        first_store = store_map.get(schs[0].store_id) if schs[0].store_id else None
        store_label = f" ({first_store.name})" if first_store else ""
        d_iso = d.isoformat()

        if total_min > OVER_HOURS_MINUTES:
            issues.append(Issue(
                key=f"over_6h|{uid}|{d_iso}",
                category="over_6h",
                target_date=d_iso,
                label=f"{user.full_name}{store_label}: {total_min / 60:.1f}h (exceeds 6h)",
                store_id=str(schs[0].store_id) if schs[0].store_id else None,
                store_name=first_store.name if first_store else None,
                shift_id=None,
                shift_name=None,
                user_id=str(uid),
                user_name=user.full_name,
                detail={"total_minutes": total_min},
            ))

        if total_min >= NO_BREAK_MINUTES and not has_break:
            issues.append(Issue(
                key=f"no_break_8h|{uid}|{d_iso}",
                category="no_break_8h",
                target_date=d_iso,
                label=f"{user.full_name}{store_label}: {total_min / 60:.1f}h without break",
                store_id=str(schs[0].store_id) if schs[0].store_id else None,
                store_name=first_store.name if first_store else None,
                shift_id=None,
                shift_name=None,
                user_id=str(uid),
                user_name=user.full_name,
                detail={"total_minutes": total_min},
            ))

    # ── 3) sv_gap — 매장 운영시간 안에서 SV 미배치 시간 구간 ──────────
    # shift 무관, 실제 schedule.start_time/end_time 기준.
    sv_by_store_date: dict[tuple[UUID, date], list[tuple[int, int]]] = {}
    for sch, _, role, _ in rows:
        if role is None or role.priority != SV_PRIORITY:
            continue
        if sch.store_id is None or sch.start_at is None or sch.end_at is None:
            continue
        # 날짜를 버리는 start_time/end_time shim 을 쓰면 안 된다 — 자정 넘김 시프트가
        # 영업일 라벨보다 하루 뒤 날짜를 갖는데 그 정보가 사라져 갭 계산이 통째로 틀어진다.
        s_m = _minutes_from_operating_day(sch.operating_day, sch.start_at)
        e_m = _minutes_from_operating_day(sch.operating_day, sch.end_at)
        sv_by_store_date.setdefault((sch.store_id, sch.operating_day), []).append((s_m, e_m))

    for store in stores:
        for d in target_dates:
            # schedule.range setting (store → org → registry default) 기반 — operating_hours JSONB 아님.
            window = await _resolve_schedule_window(db, organization_id, store.id, d)
            if window is None:
                continue  # 거의 발생 안 함 (registry default 가 있음)
            open_m, close_m = window
            sv_intervals = sv_by_store_date.get((store.id, d), [])
            gaps = _subtract_intervals(open_m, close_m, sv_intervals)
            for gs, ge in gaps:
                gap_label = f"{_minutes_to_label(gs)}–{_minutes_to_label(ge)}"
                issues.append(Issue(
                    key=f"sv_gap|{store.id}|{d.isoformat()}|{gs}-{ge}",
                    category="sv_gap",
                    target_date=d.isoformat(),
                    label=f"{store.name} {d.isoformat()} {gap_label}: no SV",
                    store_id=str(store.id),
                    store_name=store.name,
                    shift_id=None,
                    shift_name=None,
                    user_id=None,
                    user_name=None,
                    detail={
                        "start_minute": gs,
                        "end_minute": ge,
                        "duration_minutes": ge - gs,
                        "window_open": open_m,
                        "window_close": close_m,
                    },
                ))

    return stores_info, cells, issues


async def detect_issues(
    db: AsyncSession,
    organization_id: UUID,
    target_dates: list[date],
) -> list[Issue]:
    """Backward-compat wrapper — issues 만 반환."""
    _, _, issues = await collect_cells_and_issues(db, organization_id, target_dates)
    return issues


async def collect_attendance_issues(
    db: AsyncSession,
    organization_id: UUID,
    target_date: date,
) -> list[Issue]:
    """과거 1일치 attendance 기반 6h/8h 초과 — corrections 반영된 최종 상태.

    Attendance row가 update 방식으로 수정되므로 그냥 row 읽으면 최종 값.
    (AttendanceCorrection 은 audit log)
    """
    issues: list[Issue] = []

    rows = (
        await db.execute(
            select(Attendance, User, Store)
            .outerjoin(User, Attendance.user_id == User.id)
            .outerjoin(Store, Attendance.store_id == Store.id)
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date == target_date,
                Attendance.total_work_minutes.isnot(None),  # clock_out 완료된 것만
            )
        )
    ).all()

    by_user: dict[UUID, list[tuple[Attendance, User, Store | None]]] = {}
    for att, user, store in rows:
        if user is None:
            continue
        by_user.setdefault(user.id, []).append((att, user, store))

    d_iso = target_date.isoformat()
    for uid, atts in by_user.items():
        total_work = sum((a.total_work_minutes or 0) for a, _, _ in atts)
        total_break = sum((a.total_break_minutes or 0) for a, _, _ in atts)
        net = total_work - total_break
        first_user = atts[0][1]
        first_store = atts[0][2]
        store_label = f" ({first_store.name})" if first_store else ""

        if net > OVER_HOURS_MINUTES:
            issues.append(Issue(
                key=f"att_over_6h|{uid}|{d_iso}",
                category="att_over_6h",
                target_date=d_iso,
                label=f"{first_user.full_name}{store_label}: {net/60:.1f}h actual (exceeds 6h)",
                store_id=str(first_store.id) if first_store else None,
                store_name=first_store.name if first_store else None,
                shift_id=None,
                shift_name=None,
                user_id=str(uid),
                user_name=first_user.full_name,
                detail={"total_minutes": net, "source": "attendance"},
            ))

        if net >= NO_BREAK_MINUTES and total_break == 0:
            issues.append(Issue(
                key=f"att_no_break_8h|{uid}|{d_iso}",
                category="att_no_break_8h",
                target_date=d_iso,
                label=f"{first_user.full_name}{store_label}: {net/60:.1f}h actual without break",
                store_id=str(first_store.id) if first_store else None,
                store_name=first_store.name if first_store else None,
                shift_id=None,
                shift_name=None,
                user_id=str(uid),
                user_name=first_user.full_name,
                detail={"total_minutes": net, "source": "attendance"},
            ))

    return issues


# ---------------------------------------------------------------------------
# Snapshot + diff
# ---------------------------------------------------------------------------

@dataclass
class ReportDiff:
    new: list[Issue]
    resolved: list[Issue]
    ongoing: list[Issue]


async def _load_previous_snapshot(
    db: AsyncSession, organization_id: UUID
) -> ScheduleReportSnapshot | None:
    res = await db.execute(
        select(ScheduleReportSnapshot)
        .where(ScheduleReportSnapshot.organization_id == organization_id)
        .order_by(ScheduleReportSnapshot.sent_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


def observed_window(target_dates: list[date], yesterday: date | None) -> set[str]:
    """이번 실행이 실제로 관측한 날짜 집합 (ISO 문자열).

    스케줄 이슈는 target_dates, 근태 이슈는 yesterday 하루만 본다.
    """
    out = {d.isoformat() for d in target_dates}
    if yesterday is not None:
        out.add(yesterday.isoformat())
    return out


def filter_to_observed_window(previous: list[Issue], observed: set[str]) -> list[Issue]:
    """이번 실행이 보지 않은 날짜의 이전 이슈를 걷어낸다.

    매 실행마다 today 가 하루 밀리므로 두 실행의 관측 창은 서로 다르다. 창 밖 이슈를
    diff 에 넣으면 **아무도 고치지 않은 문제가 "Resolved" 로 보고된다** — 근태 이슈는
    대상 날짜가 항상 yesterday 라서 매일 전량이 그렇게 찍혔다.

    창 밖은 resolved 도 ongoing 도 아니다. "해결됐다" 와 "이제 안 본다" 는 다르다.
    """
    return [i for i in previous if i.target_date in observed]


def diff_issues(previous: list[Issue], current: list[Issue]) -> ReportDiff:
    prev_by_key = {i.key: i for i in previous}
    curr_by_key = {i.key: i for i in current}
    prev_keys = set(prev_by_key)
    curr_keys = set(curr_by_key)
    return ReportDiff(
        new=[curr_by_key[k] for k in curr_keys - prev_keys],
        resolved=[prev_by_key[k] for k in prev_keys - curr_keys],
        ongoing=[curr_by_key[k] for k in curr_keys & prev_keys],
    )


def _previous_issues_from_snapshot(snap: ScheduleReportSnapshot | None) -> list[Issue]:
    if snap is None or not snap.issues:
        return []
    return [Issue.from_jsonable(d) for d in snap.issues]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _resolve_org_today(org: Organization) -> date:
    try:
        tz = ZoneInfo(org.timezone or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def _split_recipients(raw: str | None) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


async def _resolve_recipients(db: AsyncSession, organization_id: UUID) -> list[str]:
    """수신자를 org 스코프로 해결한다.

    예전엔 전역 env(`SCHEDULE_REPORT_RECIPIENTS`) 하나였는데, 크론은 모든 org 를
    순회한다. 그래서 org 행이 하나 추가되는 것만으로 그 org 의 매장명·직원 실명·
    근무시간이 기존 org 수신자에게 배달됐다 — 코드 변경 없이 발생하는 유출이다.

    env 는 레거시 폴백으로만 남기되 **org 가 정확히 하나일 때만** 쓴다.
    org 가 둘 이상인 순간 폴백은 곧 유출이므로, 그때는 설정이 없는 org 를 건너뛴다.
    """
    try:
        raw = await resolve_setting(db, SCHEDULE_REPORT_RECIPIENTS_KEY, organization_id)
    except SettingNotRegisteredError:
        raw = None
    configured = _split_recipients(raw if isinstance(raw, str) else None)
    if configured:
        return configured

    env_recipients = _split_recipients(settings.SCHEDULE_REPORT_RECIPIENTS)
    if not env_recipients:
        return []

    org_count = await db.scalar(
        select(func.count()).select_from(Organization).where(Organization.deleted_at.is_(None))
    )
    if org_count == 1:
        logger.warning(
            "[schedule-report] org=%s 수신자가 전역 env 폴백으로 해결됨. "
            "'%s' 설정으로 옮길 것 — org 가 둘 이상이 되는 순간 이 폴백은 중단된다.",
            organization_id, SCHEDULE_REPORT_RECIPIENTS_KEY,
        )
        return env_recipients

    logger.error(
        "[schedule-report] org=%s 수신자 미설정 — org 가 %s개라 전역 env 폴백을 쓰지 않는다"
        "(크로스-org 유출 방지). '%s' 를 org 별로 설정할 것.",
        organization_id, org_count, SCHEDULE_REPORT_RECIPIENTS_KEY,
    )
    return []


def _cell_to_jsonable(c: ShiftCell) -> dict:
    d = asdict(c)
    d["target_date"] = c.target_date.isoformat()
    return d


def _cell_from_jsonable(d: dict) -> ShiftCell:
    d = dict(d)
    d["target_date"] = date.fromisoformat(d["target_date"])
    return ShiftCell(**d)


def build_render_payload(
    *,
    org_name: str,
    sent_date: date,
    target_dates: list[date],
    yesterday: date | None,
    diff: ReportDiff,
    stores: list[StoreInfo],
    cells: list[ShiftCell],
) -> dict:
    """스냅샷에 담을 렌더 재료. `render_kwargs_from_payload` 와 짝이다."""
    return {
        "org_name": org_name,
        "sent_date": sent_date.isoformat(),
        "target_dates": [d.isoformat() for d in target_dates],
        "yesterday": yesterday.isoformat() if yesterday else None,
        "stores": [asdict(s) for s in stores],
        "cells": [_cell_to_jsonable(c) for c in cells],
        "diff": {
            "new": [i.to_jsonable() for i in diff.new],
            "ongoing": [i.to_jsonable() for i in diff.ongoing],
            "resolved": [i.to_jsonable() for i in diff.resolved],
        },
    }


def render_kwargs_from_payload(payload: dict, admin_base_url: str) -> dict:
    """저장된 재료 → 이메일/PDF 빌더 kwargs.

    admin_base_url 만 지금 설정에서 가져온다 — 콘솔 주소가 바뀌면 과거 리포트의
    링크도 새 주소를 가리켜야 하기 때문이다(옛 주소로 굳으면 링크가 죽는다).
    """
    diff = payload.get("diff") or {}
    return dict(
        org_name=payload["org_name"],
        sent_date=date.fromisoformat(payload["sent_date"]),
        target_dates=[date.fromisoformat(d) for d in payload["target_dates"]],
        yesterday=date.fromisoformat(payload["yesterday"]) if payload.get("yesterday") else None,
        diff=ReportDiff(
            new=[Issue.from_jsonable(i) for i in diff.get("new", [])],
            ongoing=[Issue.from_jsonable(i) for i in diff.get("ongoing", [])],
            resolved=[Issue.from_jsonable(i) for i in diff.get("resolved", [])],
        ),
        stores=[StoreInfo(**s) for s in payload.get("stores", [])],
        cells=[_cell_from_jsonable(c) for c in payload.get("cells", [])],
        admin_base_url=admin_base_url,
    )


async def _deliver(
    recipients: list[str],
    subject: str,
    html: str,
    attachments: list[tuple[str, bytes]] | None,
) -> tuple[list[str], list[dict]]:
    """수신자별로 독립 발송. (delivered, failed) 반환.

    루프 전체를 try 하나로 감싸면 첫 주소가 거부될 때 나머지가 시도조차 못 한다.
    가드가 조용히 막은 경우(예외 없음)도 실패로 센다 — 그러지 않으면
    "보냈다고 기록됐는데 아무도 못 받은" 상태가 된다.
    """
    delivered: list[str] = []
    failed: list[dict] = []
    for to in recipients:
        try:
            actually_sent = await send_email(
                to=to, subject=subject, html=html, attachments=attachments
            )
        except Exception as e:
            failed.append({"to": to, "reason": f"{type(e).__name__}: {e}"})
            logger.exception("[schedule-report] email send failed: %s", to)
            continue
        if actually_sent:
            delivered.append(to)
        else:
            failed.append({"to": to, "reason": "blocked_by_email_guard"})
            logger.error(
                "[schedule-report] 메일이 가드에 막혀 발송되지 않음: %s "
                "(APP_ENV/EMAIL_REDIRECT_TO 확인)", to,
            )
    return delivered, failed


async def resend_last_report(
    db: AsyncSession,
    organization_id: UUID,
    *,
    recipients: list[str],
) -> dict:
    """마지막으로 **저장된** 리포트를 그대로 재발송한다. 새로 만들지 않는다.

    왜 재생성이 아닌가: 리포트 내용은 실행 시각에 의존한다(today 가 밀리고,
    그 사이 스케줄이 바뀐다). 7시에 나갔어야 할 리포트를 9시에 재생성하면
    그건 다른 문서다. 발송만 실패한 상황에서 필요한 건 "그때 그것" 이다.

    스냅샷은 절대 쓰지 않는다 — 기준선을 움직이는 주체는 크론 하나뿐이다.
    """
    snap = await _load_previous_snapshot(db, organization_id)
    if snap is None:
        return {"sent": False, "reason": "no_snapshot", "recipients": recipients}
    if not snap.payload:
        # payload 컬럼 도입 이전에 만들어진 스냅샷 — 재료가 없어 재현할 수 없다.
        return {"sent": False, "reason": "snapshot_has_no_payload", "recipients": recipients}
    if not recipients:
        return {"sent": False, "reason": "no_recipients", "recipients": []}

    render_kwargs = render_kwargs_from_payload(snap.payload, settings.ADMIN_BASE_URL)

    attachments: list[tuple[str, bytes]] | None = None
    pdf_attached = False
    if settings.SCHEDULE_REPORT_PDF:
        try:
            filename, pdf_bytes = await run_in_threadpool(
                build_schedule_daily_report_pdf, **render_kwargs
            )
            attachments = [(filename, pdf_bytes)]
            pdf_attached = True
        except Exception:
            logger.exception("[schedule-report] 재발송 PDF 렌더 실패 — 전체 본문으로 폴백")

    subject, html = build_schedule_daily_report_email(
        **render_kwargs, full=not pdf_attached
    )
    delivered, failed = await _deliver(recipients, subject, html, attachments)

    return {
        "sent": bool(delivered),
        "recipients": recipients,
        "delivered": delivered,
        "failed": failed,
        "pdf_attached": pdf_attached,
        "snapshot_saved": False,
        "generated_at": snap.sent_at.isoformat() if snap.sent_at else None,
        "issues_count": len(snap.issues or []),
        "subject": subject,
    }


async def generate_and_send_report(
    db: AsyncSession,
    organization_id: UUID,
    *,
    save_snapshot: bool = True,
    override_recipients: list[str] | None = None,
) -> dict:
    """org에 대해 보고서 생성 + (선택)스냅샷 저장 + 이메일 발송.

    Returns:
        {"sent": bool, "recipients": [...], "issues_count": int, "diff": {...}}
    """
    org = await db.get(Organization, organization_id)
    if org is None:
        raise ValueError(f"organization {organization_id} not found")

    today = _resolve_org_today(org)
    yesterday = today - timedelta(days=1)
    target_dates = [today + timedelta(days=i) for i in range(LOOKAHEAD_DAYS)]

    stores_info, cells, schedule_issues = await collect_cells_and_issues(db, organization_id, target_dates)
    attendance_issues = await collect_attendance_issues(db, organization_id, yesterday)
    current_issues = schedule_issues + attendance_issues

    prev_snap = await _load_previous_snapshot(db, organization_id)

    # 이전 실행과 이번 실행의 관측 창이 다르다 — 매 실행마다 today 가 하루 밀리기 때문이다.
    # 이번 실행이 아예 보지 않는 날짜의 이전 이슈를 diff 에 넣으면, 아무도 고치지 않은 문제가
    # 단지 창 밖으로 나갔다는 이유로 "Resolved" 로 보고된다. 근태 이슈는 대상 날짜가 항상
    # yesterday 라서 **매일 전량이** resolved 로 찍혔다.
    # 창 밖 이슈는 resolved 도 ongoing 도 아니다 — diff 에서 통째로 제외한다.
    observed_dates = observed_window(target_dates, yesterday)
    prev_all = _previous_issues_from_snapshot(prev_snap)
    prev_issues = filter_to_observed_window(prev_all, observed_dates)
    out_of_window = len(prev_all) - len(prev_issues)
    if out_of_window:
        logger.info(
            "[schedule-report] org=%s 이전 스냅샷 이슈 %d건이 이번 관측 창 밖 — diff 제외",
            organization_id, out_of_window,
        )

    diff = diff_issues(prev_issues, current_issues)

    recipients = (
        override_recipients
        if override_recipients is not None
        else await _resolve_recipients(db, organization_id)
    )

    # 본문과 PDF 는 같은 kwargs 를 쓴다 — 갈라지면 두 산출물의 숫자가 어긋난다.
    render_kwargs = dict(
        org_name=org.name,
        sent_date=today,
        target_dates=target_dates,
        yesterday=yesterday,
        diff=diff,
        stores=stores_info,
        cells=cells,
        admin_base_url=settings.ADMIN_BASE_URL,
    )

    # PDF 는 수신자가 있을 때만 렌더한다 — dry_run 과 /preview 는 override_recipients=[]
    # 로 들어오므로 미리보기에서 낭비 렌더가 없다.
    attachments: list[tuple[str, bytes]] | None = None
    pdf_attached = False
    if recipients and settings.SCHEDULE_REPORT_PDF:
        try:
            # WeasyPrint 는 동기 CPU 바운드다. 이 코드는 APScheduler 잡으로 앱 이벤트 루프
            # 위에서 도므로 threadpool 로 빼지 않으면 렌더 동안 서버 전체가 멈춘다.
            filename, pdf_bytes = await run_in_threadpool(
                build_schedule_daily_report_pdf, **render_kwargs
            )
            attachments = [(filename, pdf_bytes)]
            pdf_attached = True
        except Exception:
            # 재시도 없는 무인 크론이다. 여기서 예외를 올리면 그날 리포트가 통째로 사라진다.
            # 첨부 없이 요약만 보내는 게 최악이므로(완결돼 보이는데 상세가 없다),
            # 전체 본문으로 되돌린다 — 유일한 퇴행은 Gmail 클리핑, 즉 예전 동작 그대로다.
            logger.exception("[schedule-report] PDF 렌더 실패 — 전체 본문으로 폴백")

    subject, html = build_schedule_daily_report_email(
        **render_kwargs,
        full=not pdf_attached,
    )

    if recipients:
        delivered, failed = await _deliver(recipients, subject, html, attachments)
    else:
        delivered, failed = [], []
        logger.warning("[schedule-report] no recipients configured; skip email")

    sent_ok = bool(delivered)

    # 한 통도 못 나갔으면 베이스라인을 쓰지 않는다. 쓰면 다음 실행이 "이미 보고한 이슈"로
    # 취급해서, 발송이 복구된 날 첫 메일에 NEW 배지가 하나도 안 붙는다.
    if save_snapshot and not sent_ok:
        logger.warning(
            "[schedule-report] org=%s 발송 0건 — 스냅샷을 저장하지 않는다(베이스라인 보호)",
            organization_id,
        )
        save_snapshot = False

    if save_snapshot:
        snap = ScheduleReportSnapshot(
            organization_id=organization_id,
            sent_at=datetime.now(timezone.utc),
            target_date_from=target_dates[0],
            target_date_to=target_dates[-1],
            issues=[i.to_jsonable() for i in current_issues],
            # 재발송용 재료 — 없으면 "다시 보내기" 가 "다시 만들기" 가 되어 내용이 달라진다.
            payload=build_render_payload(
                org_name=org.name,
                sent_date=today,
                target_dates=target_dates,
                yesterday=yesterday,
                diff=diff,
                stores=stores_info,
                cells=cells,
            ),
        )
        db.add(snap)
        await db.commit()

    return {
        "sent": sent_ok,
        "recipients": recipients,
        "delivered": delivered,
        "failed": failed,
        "snapshot_saved": save_snapshot,
        "pdf_attached": pdf_attached,
        "issues_count": len(current_issues),
        "target_dates": [d.isoformat() for d in target_dates],
        "diff": {
            "new": len(diff.new),
            "resolved": len(diff.resolved),
            "ongoing": len(diff.ongoing),
        },
        "subject": subject,
        "html": html,
    }


def parse_report_hours(raw: str | None) -> list[int]:
    """"7,15,22" → [7, 15, 22]. 형태가 깨진 항목은 버리고 중복은 합친다.

    빈 값이면 빈 리스트 = 그 org 는 발송하지 않는다. 조용히 기본값으로 되돌리지 않는다 —
    "안 보내기" 는 유효한 선택이고, 그걸 못 하게 만들면 끄는 방법이 없어진다.
    """
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h = int(part)
        except ValueError:
            continue
        if 0 <= h <= 23:
            out.add(h)
    return sorted(out)


async def _resolve_report_hours(db: AsyncSession, organization_id: UUID) -> list[int]:
    try:
        raw = await resolve_setting(db, SCHEDULE_REPORT_TIMES_KEY, organization_id)
    except SettingNotRegisteredError:
        raw = None
    return parse_report_hours(raw if isinstance(raw, str) else None)


def _org_local_hour(org: Organization) -> int:
    try:
        tz = ZoneInfo(org.timezone or "UTC")
    except Exception:
        logger.warning(
            "[schedule-report] org=%s 의 timezone %r 을 해석할 수 없어 UTC 로 처리",
            org.id, org.timezone,
        )
        tz = ZoneInfo("UTC")
    return datetime.now(tz).hour


async def run_daily_report_tick(now_hour: int | None = None) -> None:
    """APScheduler 진입점 — **매시 정각**에 호출되고, 지금이 발송 시각인 org 만 보낸다.

    예전엔 고정 시각 트리거 하나였다(15시). 그러면 스케줄러의 tz 와 org 의 tz 가
    다른 순간 하루가 통째로 어긋나고, org 마다 다른 시각을 줄 방법도 없다.
    시각 판단을 org 로컬로 옮기면 둘 다 해결된다 — push_digest 가 이미 쓰는 패턴이다.

    now_hour 는 테스트 주입용. 실제 실행에서는 org timezone 기준 현재 시로 계산한다.
    """
    from app.database import async_session

    async with async_session() as db:
        orgs = (
            await db.execute(
                select(Organization).where(Organization.deleted_at.is_(None))
            )
        ).scalars().all()

    for org in orgs:
        try:
            async with async_session() as db:
                hours = await _resolve_report_hours(db, org.id)
                if not hours:
                    continue  # 이 org 는 발송 꺼둠
                hour = now_hour if now_hour is not None else _org_local_hour(org)
                if hour not in hours:
                    continue

                result = await generate_and_send_report(db, org.id)
                logger.info(
                    "[schedule-report] org=%s hour=%d issues=%d sent=%s delivered=%s failed=%s",
                    org.id, hour, result["issues_count"], result["sent"],
                    result.get("delivered"), result.get("failed"),
                )
                if not result["sent"]:
                    # 발송 0건은 성공과 같은 레벨로 흘려보내면 안 된다.
                    logger.error(
                        "[schedule-report] org=%s hour=%d 발송 0건 — failed=%s",
                        org.id, hour, result.get("failed"),
                    )
        except Exception:
            logger.exception("[schedule-report] org=%s failed", org.id)
