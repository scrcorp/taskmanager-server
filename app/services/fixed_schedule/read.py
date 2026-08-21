"""고정 근무 — 조회 시 virtual 합성 (계약 §3-1).

`merge_virtual`        GET /schedules            — 실 행 목록에 virtual(status="virtual") 항목을 합쳐 반환
`merge_virtual_roster` GET /schedules/roster     — 행/컬럼/합계에 virtual 분을 가산

억제 규칙: 실 행은 **status 불문(deleted 포함)** `(pattern_id, occurrence_date)` 로 대조해 그 슬롯의
virtual 을 지운다. 도장 없는 실 행은 억제하지 않는다(가산). virtual 은 DB 행이 아니며
id = "virtual:{pattern_id}:{date}" — PATCH/DELETE /schedules/{id} 는 UUID 파싱 실패로 404.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store
from app.models.schedule import StoreWorkRole
from app.models.user import User
from app.models.work import Position, Shift
from app.schemas.schedule import RosterResponse, ScheduleResponse
from app.services.fixed_schedule.expand import Occurrence, expand
from app.services.fixed_schedule.validation import (
    assignable_until_map,
    load_patterns_in_window,
    occupied_slots,
)
from app.utils.timezone import format_naive_iso, net_minutes_from_datetimes

VIRTUAL_PREFIX = "virtual:"


def virtual_id(pattern_id: UUID, occurrence_date: date) -> str:
    return f"{VIRTUAL_PREFIX}{pattern_id}:{occurrence_date.isoformat()}"


def is_virtual_id(value: str) -> bool:
    return value.startswith(VIRTUAL_PREFIX)


async def virtual_occurrences(
    db: AsyncSession,
    *,
    organization_id: UUID,
    date_from: date,
    date_to: date,
    store_ids: Sequence[UUID] | None = None,
    user_ids: Sequence[UUID] | None = None,
) -> list[Occurrence]:
    """창 안 패턴 펼치기 → 실 행(deleted 포함)이 점유한 슬롯 제거 → 남은 occurrence.

    materialize/overlap 검사/조회가 전부 이 한 함수를 쓴다 — 억제 판정이 갈리지 않게.
    """
    patterns = await load_patterns_in_window(
        db, organization_id=organization_id, date_from=date_from, date_to=date_to,
        user_ids=user_ids, store_ids=store_ids,
    )
    if not patterns:
        return []
    gate = await assignable_until_map(db, organization_id, [p.user_id for p in patterns])
    occs = expand(patterns, date_from=date_from, date_to=date_to, assignable_until=gate)
    if not occs:
        return []
    occupied = await occupied_slots(db, [p.id for p in patterns], date_from=date_from, date_to=date_to)
    return [o for o in occs if (o.pattern_id, o.occurrence_date) not in occupied]


async def _context_maps(db: AsyncSession, occs: Sequence[Occurrence], organization_id: UUID) -> dict[str, Any]:
    """virtual 응답 조립용 이름·시급 일괄 조회 (N+1 금지)."""
    from app.services.rate_service import rate_service

    user_ids = {o.user_id for o in occs}
    store_ids = {o.store_id for o in occs}
    role_ids = {o.work_role_id for o in occs if o.work_role_id}

    users = {r.id: r for r in (await db.execute(
        select(User.id, User.full_name, User.department).where(User.id.in_(user_ids))
    ))}
    stores = {r.id: r for r in (await db.execute(
        select(Store.id, Store.name, Store.default_hourly_rate, Store.day_start_time)
        .where(Store.id.in_(store_ids))
    ))}
    orgs = {r.id: r for r in (await db.execute(
        select(Organization.id, Organization.default_hourly_rate)
        .where(Organization.id == organization_id)
    ))}
    person_rates = await rate_service.person_rates_map(db, {(u, organization_id) for u in user_ids})

    role_names: dict[UUID, str | None] = {}
    if role_ids:
        roles = list((await db.execute(
            select(StoreWorkRole).where(StoreWorkRole.id.in_(role_ids))
        )).scalars())
        shift_ids = {r.shift_id for r in roles if not r.name and r.shift_id}
        pos_ids = {r.position_id for r in roles if not r.name and r.position_id}
        shifts = {r.id: r.name for r in (await db.execute(
            select(Shift.id, Shift.name).where(Shift.id.in_(shift_ids))
        ))} if shift_ids else {}
        positions = {r.id: r.name for r in (await db.execute(
            select(Position.id, Position.name).where(Position.id.in_(pos_ids))
        ))} if pos_ids else {}
        for r in roles:
            role_names[r.id] = r.name or f"{shifts.get(r.shift_id) or ''} - {positions.get(r.position_id) or ''}"
    return {
        "users": users, "stores": stores, "orgs": orgs,
        "person_rates": person_rates, "role_names": role_names,
    }


def _virtual_response(o: Occurrence, organization_id: UUID, ctx: dict[str, Any], *, hide_cost: bool) -> ScheduleResponse:
    from app.services.schedule_service import schedule_service

    u = ctx["users"].get(o.user_id)
    st = ctx["stores"].get(o.store_id)
    fake = SimpleNamespace(user_id=o.user_id, store_id=o.store_id, organization_id=organization_id)
    rate, source = schedule_service._resolve_rate_from_maps(fake, ctx["person_rates"], ctx["stores"], ctx["orgs"])
    now = datetime.now(timezone.utc)
    return ScheduleResponse(
        id=virtual_id(o.pattern_id, o.occurrence_date),
        organization_id=str(organization_id),
        request_id=None,
        user_id=str(o.user_id),
        user_name=u.full_name if u else None,
        user_department=u.department if u else None,
        store_id=str(o.store_id),
        store_name=st.name if st else None,
        work_role_id=str(o.work_role_id) if o.work_role_id else None,
        work_role_name=ctx["role_names"].get(o.work_role_id) if o.work_role_id else None,
        work_role_name_snapshot=ctx["role_names"].get(o.work_role_id) if o.work_role_id else None,
        position_snapshot=None,
        work_date=o.occurrence_date,
        start_time=o.start_at.strftime("%H:%M"),
        end_time=o.end_at.strftime("%H:%M"),
        break_start_time=o.break_start_at.strftime("%H:%M") if o.break_start_at else None,
        break_end_time=o.break_end_at.strftime("%H:%M") if o.break_end_at else None,
        operating_day=o.occurrence_date,
        start_at=format_naive_iso(o.start_at),
        end_at=format_naive_iso(o.end_at),
        break_start_at=format_naive_iso(o.break_start_at),
        break_end_at=format_naive_iso(o.break_end_at),
        net_work_minutes=net_minutes_from_datetimes(o.start_at, o.end_at, o.break_start_at, o.break_end_at),
        status="virtual",
        created_by=None,
        approved_by=None,
        note=None,
        hourly_rate=None if hide_cost else (rate or 0.0),
        effective_rate=None if hide_cost else rate,
        effective_rate_source=None if hide_cost else source,
        pattern_id=str(o.pattern_id),
        pattern_occurrence_date=o.occurrence_date,
        pattern_overridden=False,
        created_at=now,
        updated_at=now,
    )


async def merge_virtual(
    db: AsyncSession,
    *,
    organization_id: UUID,
    entries: list[ScheduleResponse],
    store_ids: Sequence[UUID] | None,
    date_from: date | None,
    date_to: date | None,
    hide_cost: bool,
    user_ids: Sequence[UUID] | None = None,
) -> list[ScheduleResponse]:
    """실 행 목록 + virtual → 날짜·시작 순 정렬된 하나의 목록.

    창(date_from/date_to)이 둘 다 없으면 펼칠 범위가 없으므로 실 행만 돌려준다.
    """
    if date_from is None or date_to is None:
        return entries
    occs = await virtual_occurrences(
        db, organization_id=organization_id, date_from=date_from, date_to=date_to,
        store_ids=store_ids, user_ids=user_ids,
    )
    if not occs:
        return entries
    ctx = await _context_maps(db, occs, organization_id)
    merged = list(entries) + [_virtual_response(o, organization_id, ctx, hide_cost=hide_cost) for o in occs]
    merged.sort(key=lambda r: (r.operating_day or r.work_date, r.start_at or ""))
    return merged


async def merge_virtual_roster(
    db: AsyncSession,
    *,
    organization_id: UUID,
    roster: RosterResponse,
    store_ids: Sequence[UUID] | None,
    date_from: date,
    date_to: date,
    granularity: str,
    hide_cost: bool,
) -> RosterResponse:
    """로스터 요약에 virtual 분을 confirmed 로 가산한다 — 그리드가 virtual 과 confirmed 를 동일하게
    그리므로 행/컬럼/합계도 같아야 한다. 로스터에 없는 사람(후보 밖)의 virtual 은 무시한다.
    시급은 행의 effective_hourly_rate(= 상속 시급) 를 쓴다."""
    from app.services.schedule_service import schedule_service

    rows_by_user = {r.user_id: r for r in roster.roster}
    if not rows_by_user:
        return roster
    occs = await virtual_occurrences(
        db, organization_id=organization_id, date_from=date_from, date_to=date_to,
        store_ids=store_ids, user_ids=[UUID(u) for u in rows_by_user],
    )
    occs = [o for o in occs if str(o.user_id) in rows_by_user]
    if not occs:
        return roster

    # 숨긴 시급이면 비용은 None 유지 → 0 으로 계산하되 결과에 싣지 않는다
    def _rate(o: Occurrence) -> float:
        r = rows_by_user[str(o.user_id)].effective_hourly_rate
        return float(r or 0.0)

    fakes = []
    for o in occs:
        net = net_minutes_from_datetimes(o.start_at, o.end_at, o.break_start_at, o.break_end_at)
        fakes.append(SimpleNamespace(
            id=virtual_id(o.pattern_id, o.occurrence_date),
            user_id=o.user_id, operating_day=o.occurrence_date, work_date=o.occurrence_date,
            start_at=o.start_at, end_at=o.end_at,
            start_time=o.start_at.time(), end_time=o.end_at.time(),
            net_work_minutes=net, hourly_rate=_rate(o), status="confirmed",
        ))
        row = rows_by_user[str(o.user_id)]
        hrs = net / 60.0
        row.has_schedule_in_period = True
        row.confirmed_hours = round(row.confirmed_hours + hrs, 2)
        if not hide_cost and row.confirmed_cost is not None:
            row.confirmed_cost = round(row.confirmed_cost + hrs * _rate(o), 2)

    # 컬럼 — virtual 만으로 같은 빌더를 돌려 키별로 더한다
    vcols = {c.key: c for c in schedule_service._roster_columns(fakes, granularity, date_from, date_to, hide_cost)}
    existing = {c.key: c for c in roster.columns}
    for key, vc in vcols.items():
        c = existing.get(key)
        if c is None:
            roster.columns.append(vc)
            continue
        c.team_confirmed = round(c.team_confirmed + vc.team_confirmed, 2)
        c.hours_confirmed = round(c.hours_confirmed + vc.hours_confirmed, 2)
        if not hide_cost and c.cost_confirmed is not None and vc.cost_confirmed is not None:
            c.cost_confirmed = round(c.cost_confirmed + vc.cost_confirmed, 2)
        if vc.slots_confirmed:
            base = c.slots_confirmed or [0, 0]
            c.slots_confirmed = [base[0] + vc.slots_confirmed[0], base[1] + vc.slots_confirmed[1]]
    if granularity == "day":
        roster.columns.sort(key=lambda c: int(c.key[1:]) if c.key.startswith("h") else 0)

    # 합계
    tot_h = sum(f.net_work_minutes for f in fakes) / 60.0
    tot_c = sum(f.net_work_minutes / 60.0 * f.hourly_rate for f in fakes)
    t = roster.totals
    t.team_confirmed = round(t.team_confirmed + len(fakes), 2)
    t.hours_confirmed = round(t.hours_confirmed + tot_h, 2)
    if not hide_cost and t.cost_confirmed is not None:
        t.cost_confirmed = round(t.cost_confirmed + tot_c, 2)
    t.staff_count = len({r.user_id for r in roster.roster if r.has_schedule_in_period})
    roster.roster.sort(key=lambda r: (not r.has_schedule_in_period, (r.user_name or "").lower()))
    return roster
