"""고정 근무 패턴 — 겹침·가용성 검사 (계약 §3-3).

① 창 안 블록끼리          `block_overlap_issues(blocks)`                       → 400 PATTERN_BLOCK_OVERLAP
② 다른 그룹 패턴과        `find_overlapping_groups(db, ...)`                   → 409 PATTERN_OVERLAP_EXISTING
③ 개별 스케줄 쓰기 시     `pattern_overlap_warnings(db, ...)`                  → OVERLAPPING_SCHEDULE + source:"pattern" (경고)
④ availability(D-f)       `availability_issues(db, ...)`                       → 400 PATTERN_OUTSIDE_AVAILABILITY

규칙 메모
- 요일은 0=Sun..6=Sat. 파이썬 weekday(0=Mon) 와 섞지 않는다 (`expand.dow_sun0`).
- 시간 겹침은 "같은 요일 ∧ 분 구간 겹침" 이다. overnight(end<=start) 는 end+24h 로 편다.
  2교대(시간이 안 겹치는 같은 요일 블록)는 허용.
- ④ 는 **패턴 저장에만** 적용한다. 일반 스케줄(`schedule_service._validate_entry`)은 이 검사를
  타지 않는다 — 여기서 export 하는 것 중 `_validate_entry` 가 쓰는 건 ③ 하나뿐이다.
- 이 모듈은 `schedule_service` 가 import 한다 → 여기서 `schedule_service` 를 모듈 레벨로
  import 하면 순환이다. 금지.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from typing import Any, Iterator, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import schedule_codes as codes
from app.core.error_codes.fixed_schedule import (
    PATTERN_BLOCK_OVERLAP,
    PATTERN_OUTSIDE_AVAILABILITY,
)
from app.models.availability import StaffAvailability
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.work_pattern import StaffWorkPattern
from app.schemas.schedule_pattern import PatternBlockIn
from app.services import staff_assignment_service
from app.services.fixed_schedule.expand import expand

DAY_MIN = 24 * 60


# ─── 시간 구간 도우미 ─────────────────────────────────────────────


def _hhmm_to_min(v: str | time) -> int:
    if isinstance(v, time):
        return v.hour * 60 + v.minute
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def span_minutes(start: str | time, end: str | time) -> tuple[int, int]:
    """[start, end) 분 구간. overnight(end <= start) 면 end 에 +24h."""
    s = _hhmm_to_min(start)
    e = _hhmm_to_min(end)
    if e <= s:
        e += DAY_MIN
    return s, e


def spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _block_period(block: PatternBlockIn, start_date: date, until_date: date | None) -> tuple[date, date | None]:
    return (block.start_date or start_date, block.until_date if block.until_date is not None else until_date)


def periods_intersect(a: tuple[date, date | None], b: tuple[date, date | None]) -> bool:
    """[start, until] (until None = ∞) 두 기간의 교차."""
    a_end = a[1] or date.max
    b_end = b[1] or date.max
    return a[0] <= b_end and b[0] <= a_end


# ─── ① 창 안 블록끼리 ───────────────────────────────────────────


def block_overlap_issues(blocks: Sequence[PatternBlockIn], *, start_date: date | None = None,
                         until_date: date | None = None) -> list[dict[str, Any]]:
    """같은 요일 ∧ 시간 겹침인 블록 쌍을 `{code, params:{blocks:[i,j], dow}}` 로 모은다.

    기간이 서로 안 겹치는 블록(블록별 "Different period")은 같은 요일이어도 충돌이 아니다.
    start_date 를 안 주면 기간은 보지 않는다(순수 요일·시간 검사).
    """
    issues: list[dict[str, Any]] = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            bi, bj = blocks[i], blocks[j]
            if start_date is not None and not periods_intersect(
                _block_period(bi, start_date, until_date), _block_period(bj, start_date, until_date)
            ):
                continue
            shared = sorted(set(bi.byday) & set(bj.byday))
            if not shared:
                continue
            if not spans_overlap(span_minutes(bi.start_time, bi.end_time), span_minutes(bj.start_time, bj.end_time)):
                continue
            for dow in shared:
                issues.append(PATTERN_BLOCK_OVERLAP.issue(blocks=[i, j], dow=dow))
    return issues


# ─── ② 다른 그룹 패턴과 ─────────────────────────────────────────


async def find_overlapping_groups(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    store_id: UUID,
    blocks: Sequence[PatternBlockIn],
    start_date: date,
    until_date: date | None,
    exclude_group_id: UUID | None = None,
) -> dict[UUID, list[StaffWorkPattern]]:
    """같은 user·store 의 다른 그룹 중 (요일 ∧ 시간 ∧ 기간) 이 겹치는 그룹 → {group_id: [그 그룹 전체 행]}."""
    rows = (await db.execute(
        select(StaffWorkPattern).where(
            StaffWorkPattern.organization_id == organization_id,
            StaffWorkPattern.user_id == user_id,
            StaffWorkPattern.store_id == store_id,
        ).order_by(StaffWorkPattern.start_date, StaffWorkPattern.start_time)
    )).scalars().all()
    if exclude_group_id is not None:
        rows = [r for r in rows if r.group_id != exclude_group_id]
    if not rows:
        return {}

    hit_groups: set[UUID] = set()
    for r in rows:
        r_span = span_minutes(r.start_time, r.end_time)
        r_period = (r.start_date, r.until_date)
        for b in blocks:
            if not (set(b.byday) & set(r.byday)):
                continue
            if not periods_intersect(_block_period(b, start_date, until_date), r_period):
                continue
            if spans_overlap(span_minutes(b.start_time, b.end_time), r_span):
                hit_groups.add(r.group_id)
                break

    out: dict[UUID, list[StaffWorkPattern]] = {}
    for r in rows:
        if r.group_id in hit_groups:
            out.setdefault(r.group_id, []).append(r)
    return out


# ─── ④ availability (D-f) ───────────────────────────────────────


def _inside(block: tuple[int, int], rng: tuple[int, int]) -> bool:
    """블록이 range 안에 들어가는가. 둘 다 overnight 로 펼쳐진 분 구간.
    블록을 +24h 민 버전도 본다 — range 20:00–04:00 안의 01:00–03:00 블록 같은 경우."""
    for shift in (0, DAY_MIN):
        s, e = block[0] + shift, block[1] + shift
        if s >= rng[0] and e <= rng[1]:
            return True
    return False


async def availability_issues(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    blocks: Sequence[PatternBlockIn],
) -> list[dict[str, Any]]:
    """staff_availability 대조. **행 없음 / 요일 행 없음 = 제약 없음.** off 또는 range 밖 = 위반.

    params: {dow, block} — UI 가 해당 블록의 요일 버튼을 빨갛게 칠한다.
    """
    rows = (await db.execute(
        select(StaffAvailability).where(
            StaffAvailability.user_id == user_id,
            StaffAvailability.organization_id == organization_id,
        )
    )).scalars().all()
    if not rows:
        return []
    by_dow = {r.day_of_week: r for r in rows}

    issues: list[dict[str, Any]] = []
    for idx, b in enumerate(blocks):
        b_span = span_minutes(b.start_time, b.end_time)
        for dow in b.byday:
            av = by_dow.get(dow)
            if av is None or av.state == "full":
                continue
            if av.state == "off":
                issues.append(PATTERN_OUTSIDE_AVAILABILITY.issue(dow=dow, block=idx))
                continue
            # range
            if av.start_time is None or av.end_time is None:
                continue
            if not _inside(b_span, span_minutes(av.start_time, av.end_time)):
                issues.append(PATTERN_OUTSIDE_AVAILABILITY.issue(dow=dow, block=idx))
    return issues


# ─── ③ 개별 스케줄 쓰기 시 패턴(virtual)과 겹침 ─────────────────

# 실체화 중인 슬롯 — 자기 자신의 virtual 과 겹친다는 경고를 내지 않기 위해
# materialize 가 create_entry 호출을 이 컨텍스트로 감싼다. `_validate_entry` 의 시그니처를
# 건드리지 않고(허용 수정 = 끝 1줄) 도장을 전달하는 유일한 통로.
_materializing_slot: ContextVar[tuple[UUID, date] | None] = ContextVar(
    "fixed_schedule_materializing_slot", default=None
)


@contextmanager
def materializing(pattern_id: UUID, occurrence_date: date) -> Iterator[None]:
    token = _materializing_slot.set((pattern_id, occurrence_date))
    try:
        yield
    finally:
        _materializing_slot.reset(token)


async def occupied_slots(
    db: AsyncSession,
    pattern_ids: Sequence[UUID],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> set[tuple[UUID, date]]:
    """실 행이 점유한 `(pattern_id, occurrence_date)` — **status 불문(deleted 포함)**."""
    ids = list(dict.fromkeys(pattern_ids))
    if not ids:
        return set()
    q = select(Schedule.pattern_id, Schedule.pattern_occurrence_date).where(
        Schedule.pattern_id.in_(ids)
    )
    if date_from is not None:
        q = q.where(Schedule.pattern_occurrence_date >= date_from)
    if date_to is not None:
        q = q.where(Schedule.pattern_occurrence_date <= date_to)
    rows = (await db.execute(q)).all()
    return {(pid, d) for pid, d in rows if pid is not None and d is not None}


async def load_patterns_in_window(
    db: AsyncSession,
    *,
    organization_id: UUID,
    date_from: date,
    date_to: date,
    user_ids: Sequence[UUID] | None = None,
    store_ids: Sequence[UUID] | None = None,
) -> list[StaffWorkPattern]:
    """창과 기간이 교차하는 패턴 행."""
    q = select(StaffWorkPattern).where(
        StaffWorkPattern.organization_id == organization_id,
        StaffWorkPattern.start_date <= date_to,
        (StaffWorkPattern.until_date.is_(None)) | (StaffWorkPattern.until_date >= date_from),
    )
    if user_ids is not None:
        if not user_ids:
            return []
        q = q.where(StaffWorkPattern.user_id.in_(list(user_ids)))
    if store_ids is not None:
        if not store_ids:
            return []
        q = q.where(StaffWorkPattern.store_id.in_(list(store_ids)))
    return list((await db.execute(q.order_by(StaffWorkPattern.start_date))).scalars().all())


async def assignable_until_map(
    db: AsyncSession, organization_id: UUID, user_ids: Sequence[UUID],
) -> dict[UUID, date | None]:
    """`expand` 가 받는 모양으로 변환. employed=False 는 **키를 빼서** 차단(fail-closed)한다.
    (Assignability.assignable_until 은 미고용자도 None 이라 그대로 넘기면 무제한이 돼버린다.)"""
    amap = await staff_assignment_service.get_assignability(db, organization_id, user_ids)
    return {uid: a.assignable_until for uid, a in amap.items() if a.employed}


async def pattern_overlap_warnings(
    db: AsyncSession,
    user_id: UUID,
    store_id: UUID,
    operating_day: date | None,
    start_at: datetime | None,
    end_at: datetime | None,
    *,
    exclude_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """해당 날짜 ±1일의 패턴 펼치기(억제 적용 후)와 후보 구간의 겹침 → OVERLAPPING_SCHEDULE 경고.

    `_validate_entry` 끝에서 1회 호출된다. 실 행과의 겹침은 기존 `check_time_overlap` 이 이미
    보므로 여기선 **virtual 만** 본다 — 실 행이 점유한 슬롯은 제외(deleted 포함).
    exclude_id: 수정 중인 행. 그 행이 도장 행이면 자기 슬롯을 점유 목록에서 빼지 않는다
    (자기 슬롯은 이미 실 행이라 virtual 이 아니다 — 결과적으로 무관).
    """
    if operating_day is None or start_at is None or end_at is None:
        return []
    org_id = await db.scalar(select(Store.organization_id).where(Store.id == store_id))
    if org_id is None:
        return []
    lo, hi = operating_day - timedelta(days=1), operating_day + timedelta(days=1)
    # 사람 단위(매장 불문) — 한 사람이 두 매장에서 같은 시간에 일할 수 없다는 점은 실 행 겹침과 같다.
    patterns = await load_patterns_in_window(
        db, organization_id=org_id, date_from=lo, date_to=hi, user_ids=[user_id],
    )
    if not patterns:
        return []
    gate = await assignable_until_map(db, org_id, [user_id])
    occs = expand(patterns, date_from=lo, date_to=hi, assignable_until=gate)
    if not occs:
        return []
    occupied = await occupied_slots(db, [p.id for p in patterns], date_from=lo, date_to=hi)
    skip = _materializing_slot.get()
    warnings: list[dict[str, Any]] = []
    for o in occs:
        key = (o.pattern_id, o.occurrence_date)
        if key in occupied or key == skip:
            continue
        if start_at < o.end_at and o.start_at < end_at:
            warnings.append(codes.issue(
                codes.OVERLAPPING_SCHEDULE,
                user_id=str(user_id),
                source="pattern",
                pattern_id=str(o.pattern_id),
                occurrence_date=o.occurrence_date.isoformat(),
            ))
    return warnings
