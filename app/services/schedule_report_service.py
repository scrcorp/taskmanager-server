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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permissions import SV_PRIORITY
from app.models.attendance import Attendance
from app.models.organization import Organization, ShiftPreset, Store
from app.models.schedule import Schedule, StoreWorkRole
from app.models.schedule_report import ScheduleReportSnapshot
from app.models.user import Role, User
from app.models.work import Shift
from app.utils.email import send_email
from app.utils.email_templates import build_schedule_daily_report_email
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting

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

# operating_hours JSONB 요일 키. date.weekday(): Mon=0..Sun=6
_DOW_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_time_str(s: str | None) -> time | None:
    if not s:
        return None
    try:
        return time.fromisoformat(s)
    except Exception:
        return None


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


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


def _operating_window_minutes(
    operating_hours: dict | None,
    target_date: date,
) -> tuple[int, int] | None:
    """[deprecated for SV gap] Store.operating_hours JSONB 기반 — _shift_within_operating_hours 만 사용.

    SV gap 검사는 _extract_schedule_range() + settings.schedule.range 를 사용한다.
    """
    if not operating_hours or not isinstance(operating_hours, dict):
        return None
    day_key = _DOW_KEYS[target_date.weekday()]
    hours = operating_hours.get(day_key)
    if not hours or not isinstance(hours, dict):
        return None
    open_t = _parse_time_str(hours.get("open") or hours.get("start"))
    close_t = _parse_time_str(hours.get("close") or hours.get("end"))
    if open_t is None or close_t is None:
        return None
    open_m = _time_to_minutes(open_t)
    close_m = _time_to_minutes(close_t)
    if close_m <= open_m:
        close_m += 1440
    return (open_m, close_m)


def _parse_minutes_str(s: str | None) -> int | None:
    """'HH:MM' → 분. 24:00 같은 자정 끝 표기 지원 (time.fromisoformat 가 막는 케이스)."""
    if not s or not isinstance(s, str):
        return None
    try:
        hh, mm = s.split(":", 1)
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 48 and 0 <= m < 60):
        return None
    return h * 60 + m


def _parse_range_pair(start_str: str | None, end_str: str | None) -> tuple[int, int] | None:
    s_m = _parse_minutes_str(start_str)
    e_m = _parse_minutes_str(end_str)
    if s_m is None or e_m is None:
        return None
    if e_m <= s_m:
        e_m += 1440
    return (s_m, e_m)


def _extract_schedule_range(value: dict | None, target_date: date) -> tuple[int, int] | None:
    """settings.schedule.range 값에서 (open_min, close_min) 추출 — console 의 extractRange 와 동일 로직.

    형식:
      - {"all": {"start": "HH:MM", "end": "HH:MM"}}
      - {"mode": "per_day", "per_day": {"mon": {...}, ...}}
      - 레거시: {"mon": {...}, "all": {...}}
    """
    if not value or not isinstance(value, dict):
        return None
    dow_key = _DOW_KEYS[target_date.weekday()]
    mode = value.get("mode")

    # per_day 모드
    if mode == "per_day":
        per_day = value.get("per_day")
        if isinstance(per_day, dict):
            entry = per_day.get(dow_key)
            if isinstance(entry, dict):
                got = _parse_range_pair(entry.get("start"), entry.get("end"))
                if got:
                    return got
            # 콘솔과 동일 — 그 요일 미설정 시 전체 min/max fallback
            starts, ends = [], []
            for v in per_day.values():
                if isinstance(v, dict):
                    pair = _parse_range_pair(v.get("start"), v.get("end"))
                    if pair:
                        starts.append(pair[0])
                        ends.append(pair[1])
            if starts and ends:
                return (min(starts), max(ends))

    # "all" 모드 (또는 단순 형식)
    all_entry = value.get("all")
    if isinstance(all_entry, dict):
        got = _parse_range_pair(all_entry.get("start"), all_entry.get("end"))
        if got:
            return got

    # 레거시 top-level 요일 키
    entry = value.get(dow_key)
    if isinstance(entry, dict):
        got = _parse_range_pair(entry.get("start"), entry.get("end"))
        if got:
            return got
    return None


async def _resolve_schedule_window(
    db: AsyncSession,
    organization_id: UUID,
    store_id: UUID,
    target_date: date,
) -> tuple[int, int] | None:
    """매장 → org → registry default cascade 로 schedule.range 해석."""
    try:
        raw = await resolve_setting(db, "schedule.range", organization_id, store_id)
    except SettingNotRegisteredError:
        return None
    return _extract_schedule_range(raw, target_date)


def _shift_within_operating_hours(
    store_operating_hours: dict | None,
    target_date: date,
    shift_start: time | None,
    shift_end: time | None,
) -> bool:
    """매장 운영시간 + 시프트 시간 교차 판단.

    Returns True 이면 검사 대상. False 이면 운영시간 외 — 스킵.
    operating_hours 미설정/시프트 시간 미설정 시 보수적으로 True (검사).
    """
    if not store_operating_hours or not isinstance(store_operating_hours, dict):
        return True
    day_key = _DOW_KEYS[target_date.weekday()]
    hours = store_operating_hours.get(day_key)
    if not hours or not isinstance(hours, dict):
        return False  # 그 요일 휴무
    open_t = _parse_time_str(hours.get("open") or hours.get("start"))
    close_t = _parse_time_str(hours.get("close") or hours.get("end"))
    if open_t is None or close_t is None:
        return True
    if shift_start is None or shift_end is None:
        return True
    # 시프트가 운영시간 안에 있어야 함 (자정 넘김은 단순화: 그대로 비교)
    return shift_start >= open_t and shift_end <= close_t


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

    # 매장이 정의한 모든 shifts 가 후보. work_role/schedule 유무와 무관.
    # → 같은 매장이라면 시프트 정의가 동일하게 표시되도록 일관성 보장.
    pending_cells: list[tuple[UUID, UUID, date, Store, Shift]] = []
    for shift in shifts:
        store = store_map.get(shift.store_id)
        if not store:
            continue
        s_start, s_end = shift_window.get((shift.store_id, shift.id), (None, None))
        for d in target_dates:
            if not _shift_within_operating_hours(store.operating_hours, d, s_start, s_end):
                continue
            pending_cells.append((shift.store_id, shift.id, d, store, shift))

    rows = (
        await db.execute(
            select(Schedule, User, Role)
            .outerjoin(User, Schedule.user_id == User.id)
            .outerjoin(Role, User.role_id == Role.id)
            .where(
                Schedule.organization_id == organization_id,
                Schedule.work_date.in_(target_dates),
                Schedule.status.in_(CONFIRMED_STATUSES),
            )
        )
    ).all()

    # 1) shift_understaffed + sv_missing — (store, shift, date) 그룹
    by_shift: dict[tuple[UUID, UUID, date], list[tuple[Schedule, User | None, Role | None]]] = {}
    for sch, user, role in rows:
        if sch.store_id is None or sch.shift_id is None:
            continue
        by_shift.setdefault((sch.store_id, sch.shift_id, sch.work_date), []).append((sch, user, role))

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
        if not members:
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
    for sch, user, _ in rows:
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
    for sch, _, role in rows:
        if role is None or role.priority != SV_PRIORITY:
            continue
        if sch.store_id is None or sch.start_time is None or sch.end_time is None:
            continue
        s_m = _time_to_minutes(sch.start_time)
        e_m = _time_to_minutes(sch.end_time)
        if e_m <= s_m:
            e_m += 1440
        sv_by_store_date.setdefault((sch.store_id, sch.work_date), []).append((s_m, e_m))

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


def _recipients() -> list[str]:
    raw = (settings.SCHEDULE_REPORT_RECIPIENTS or "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


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
    prev_issues = _previous_issues_from_snapshot(prev_snap)
    diff = diff_issues(prev_issues, current_issues)

    recipients = override_recipients if override_recipients is not None else _recipients()

    subject, html = build_schedule_daily_report_email(
        org_name=org.name,
        sent_date=today,
        target_dates=target_dates,
        yesterday=yesterday,
        diff=diff,
        stores=stores_info,
        cells=cells,
        admin_base_url=settings.ADMIN_BASE_URL,
    )

    sent_ok = False
    if recipients:
        try:
            for to in recipients:
                await send_email(to=to, subject=subject, html=html)
            sent_ok = True
        except Exception:
            logger.exception("[schedule-report] email send failed")
    else:
        logger.warning("[schedule-report] no recipients configured; skip email")

    if save_snapshot:
        snap = ScheduleReportSnapshot(
            organization_id=organization_id,
            sent_at=datetime.now(timezone.utc),
            target_date_from=target_dates[0],
            target_date_to=target_dates[-1],
            issues=[i.to_jsonable() for i in current_issues],
        )
        db.add(snap)
        await db.commit()

    return {
        "sent": sent_ok,
        "recipients": recipients,
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


async def run_daily_report_tick() -> None:
    """APScheduler 진입점 — 모든 활성 org에 대해 보고서 발송."""
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
                result = await generate_and_send_report(db, org.id)
                logger.info(
                    "[schedule-report] org=%s issues=%d sent=%s",
                    org.id, result["issues_count"], result["sent"],
                )
        except Exception:
            logger.exception("[schedule-report] org=%s failed", org.id)
