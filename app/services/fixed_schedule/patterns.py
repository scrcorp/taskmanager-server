"""고정 근무 — 패턴 그룹 서비스 (계약 §3-2).

create_group / update_group / move_group / delete_group / list_for_user / validate_group
materialize_occurrence / revert_to_pattern

원칙
- 패턴 원장(`staff_work_patterns`)은 이 모듈이 직접 쓴다. **schedules 쓰기는
  `schedule_service.create_entry / update_entry / delete_entry` 만** — repository 직접·raw INSERT 금지.
- 도장 컬럼(pattern_id / pattern_occurrence_date / pattern_overridden)은 이 도메인 소유 → ORM 직접 set.
- `create_entry` 등이 건별 commit 하므로 패턴 행은 실체화 **전에** commit 한다(§3-4 메모).
- 요일 0=Sun..6=Sat. rrule 과 byday 는 항상 함께 쓴다(`_rrule`).
- 모든 진입점은 `organization_id` 스코프를 받는다 — 다른 org 의 group_id 는 404.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Literal, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes.fixed_schedule import (
    PATTERN_BLOCK_OVERLAP,
    PATTERN_BLOCK_PERIOD_INVALID,
    PATTERN_DAY_REMOVED,
    PATTERN_GROUP_STARTED,
    PATTERN_MOVE_INTO_PAST,
    PATTERN_MOVE_PAST_END,
    PATTERN_NO_OCCURRENCE,
    PATTERN_NOT_FOUND,
    PATTERN_OUTSIDE_AVAILABILITY,
    PATTERN_OVERLAP_EXISTING,
    PATTERN_PATCH_REQUIRED,
    PATTERN_REVERT_NOT_OVERRIDDEN,
    PATTERN_SUBJECT_IMMUTABLE,
)
from app.models.organization import Store
from app.models.schedule import Schedule, StoreWorkRole
from app.models.user import User
from app.models.work_pattern import StaffWorkPattern
from app.repositories.schedule_repository import schedule_repository
from app.schemas.schedule import ScheduleResponse, ScheduleUpdate
from app.schemas.schedule_pattern import (
    PatternBlockIn,
    PatternBlockOut,
    PatternGroupIn,
    PatternGroupOut,
    PatternIssue,
    PatternValidateOut,
)
from app.services.fixed_schedule.expand import expand
from app.services.fixed_schedule.materialize import (
    _create_payload,
    materialize_window,
    org_today,
    patch_from_occurrence,
    sweep_group,
    window_end,
)
from app.services.fixed_schedule.validation import (
    assignable_until_map,
    availability_issues,
    block_overlap_issues,
    find_overlapping_groups,
    materializing,
)

_RRULE_DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]  # index = 0=Sun..6=Sat


def _rrule(byday: Sequence[int]) -> str:
    return "FREQ=WEEKLY;BYDAY=" + ",".join(_RRULE_DAYS[d] for d in sorted(byday))


def _t(hhmm: str | None) -> time | None:
    if hhmm is None:
        return None
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


# ─── 조회/응답 조립 ──────────────────────────────────────────────


async def _role_names(db: AsyncSession, role_ids: set[UUID]) -> dict[UUID, str | None]:
    if not role_ids:
        return {}
    rows = (await db.execute(select(StoreWorkRole.id, StoreWorkRole.name).where(StoreWorkRole.id.in_(role_ids)))).all()
    return {r.id: r.name for r in rows}


def _block_out(r: StaffWorkPattern, role_names: dict[UUID, str | None]) -> PatternBlockOut:
    return PatternBlockOut(
        id=str(r.id),
        work_role_id=str(r.work_role_id) if r.work_role_id else None,
        work_role_name=role_names.get(r.work_role_id) if r.work_role_id else None,
        rrule=r.rrule,
        byday=sorted(r.byday),
        start_time=_hhmm(r.start_time) or "",
        end_time=_hhmm(r.end_time) or "",
        break_start_time=_hhmm(r.break_start_time),
        break_end_time=_hhmm(r.break_end_time),
        start_date=r.start_date,
        until_date=r.until_date,
    )


def _is_ended(r: StaffWorkPattern, today: date) -> bool:
    return r.until_date is not None and r.until_date < today


async def _group_out(
    db: AsyncSession,
    rows: Sequence[StaffWorkPattern],
    *,
    include_ended: bool = True,
    today: date | None = None,
) -> PatternGroupOut:
    today = today or await org_today(db, rows[0].organization_id)
    rows = sorted(rows, key=lambda r: (r.start_date, r.start_time))
    shown = rows if include_ended else [r for r in rows if not _is_ended(r, today)]
    if not shown:
        shown = rows
    first = rows[0]
    user_name = await db.scalar(select(User.full_name).where(User.id == first.user_id))
    store_name = await db.scalar(select(Store.name).where(Store.id == first.store_id))
    names = await _role_names(db, {r.work_role_id for r in shown if r.work_role_id})
    untils = [r.until_date for r in shown]
    return PatternGroupOut(
        group_id=str(first.group_id),
        user_id=str(first.user_id),
        user_name=user_name,
        store_id=str(first.store_id),
        store_name=store_name,
        start_date=min(r.start_date for r in shown),
        until_date=None if any(u is None for u in untils) else max(untils),  # type: ignore[type-var]
        blocks=[_block_out(r, names) for r in shown],
        created_at=min(r.created_at for r in rows),
    )


async def _load_group(db: AsyncSession, group_id: UUID, organization_id: UUID) -> list[StaffWorkPattern]:
    rows = list((await db.execute(
        select(StaffWorkPattern).where(
            StaffWorkPattern.group_id == group_id,
            StaffWorkPattern.organization_id == organization_id,
        ).order_by(StaffWorkPattern.start_date, StaffWorkPattern.start_time)
    )).scalars())
    if not rows:
        raise PATTERN_NOT_FOUND(group_id=str(group_id))
    return rows


def _started(rows: Sequence[StaffWorkPattern], today: date) -> bool:
    return min(r.start_date for r in rows) <= today


async def list_for_user(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    store_id: UUID | None = None,
    include_ended: bool = False,
) -> list[PatternGroupOut]:
    """그룹 단위 목록 — 기본은 현재 유효 + 예정만(끝난 블록은 숨김, 전부 끝난 그룹은 제외)."""
    today = await org_today(db, organization_id)
    q = select(StaffWorkPattern).where(
        StaffWorkPattern.organization_id == organization_id,
        StaffWorkPattern.user_id == user_id,
    )
    if store_id is not None:
        q = q.where(StaffWorkPattern.store_id == store_id)
    rows = list((await db.execute(q.order_by(StaffWorkPattern.start_date, StaffWorkPattern.start_time))).scalars())
    groups: dict[UUID, list[StaffWorkPattern]] = {}
    for r in rows:
        groups.setdefault(r.group_id, []).append(r)
    out: list[PatternGroupOut] = []
    for g_rows in groups.values():
        if not include_ended and all(_is_ended(r, today) for r in g_rows):
            continue
        out.append(await _group_out(db, g_rows, include_ended=include_ended, today=today))
    out.sort(key=lambda g: (g.start_date, g.created_at))
    return out


# ─── 검증 ───────────────────────────────────────────────────────


async def _check_blocks(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    data: PatternGroupIn,
) -> None:
    """① 블록 겹침, ④ availability → 400. (② 는 호출자가 gate 와 함께 다룬다)"""
    issues = block_overlap_issues(data.blocks, start_date=data.start_date, until_date=data.until_date)
    if issues:
        first = issues[0]["params"]
        raise PATTERN_BLOCK_OVERLAP(blocks=first["blocks"], dow=first["dow"], issues=issues)
    av = await availability_issues(db, organization_id=organization_id, user_id=user_id, blocks=data.blocks)
    if av:
        first = av[0]["params"]
        raise PATTERN_OUTSIDE_AVAILABILITY(dow=first["dow"], block=first["block"], issues=av)


async def validate_group(
    db: AsyncSession,
    *,
    organization_id: UUID,
    data: PatternGroupIn,
    exclude_group_id: UUID | None = None,
) -> PatternValidateOut:
    """저장 없이 ①②④ — `POST /schedules/patterns/validate`."""
    user_id, store_id = UUID(data.user_id), UUID(data.store_id)
    errors = block_overlap_issues(data.blocks, start_date=data.start_date, until_date=data.until_date)
    errors += await availability_issues(db, organization_id=organization_id, user_id=user_id, blocks=data.blocks)
    hits = await find_overlapping_groups(
        db, organization_id=organization_id, user_id=user_id, store_id=store_id,
        blocks=data.blocks, start_date=data.start_date, until_date=data.until_date,
        exclude_group_id=exclude_group_id,
    )
    overlaps = [await _group_out(db, rows) for rows in hits.values()]
    return PatternValidateOut(errors=[PatternIssue(**e) for e in errors], overlaps=overlaps)


# ─── 행 생성 ─────────────────────────────────────────────────────


def _build_rows(
    *,
    organization_id: UUID,
    group_id: UUID,
    actor: User | None,
    data: PatternGroupIn,
    floor_start: date | None = None,
) -> list[StaffWorkPattern]:
    """블록 N개 → 행 N개. floor_start 가 있으면 start_date 를 그 아래로 내려가지 않게 올린다(진행 중 수정)."""
    user_id, store_id = UUID(data.user_id), UUID(data.store_id)
    rows: list[StaffWorkPattern] = []
    for b in data.blocks:
        start = b.start_date or data.start_date
        if floor_start is not None and start < floor_start:
            start = floor_start
        until = b.until_date if b.until_date is not None else data.until_date
        if until is not None and until < start:
            raise PATTERN_BLOCK_PERIOD_INVALID(start_date=start.isoformat(), until_date=until.isoformat())
        rows.append(StaffWorkPattern(
            id=uuid.uuid4(),
            organization_id=organization_id,
            store_id=store_id,
            user_id=user_id,
            group_id=group_id,
            work_role_id=UUID(b.work_role_id) if b.work_role_id else None,
            rrule=_rrule(b.byday),
            byday=sorted(b.byday),
            start_time=_t(b.start_time),
            end_time=_t(b.end_time),
            break_start_time=_t(b.break_start_time),
            break_end_time=_t(b.break_end_time),
            start_date=start,
            until_date=until,
            created_by=actor.id if actor else None,
        ))
    return rows


async def _unstamp_rows(db: AsyncSession, pattern_ids: Sequence[UUID]) -> None:
    """패턴 행을 지우기 **전에** 그 패턴을 가리키는 실 행의 도장을 전부 푼다 → 일회성 행.

    FK 는 SET NULL 이지만 ck_schedules_pattern_pair 가 (pattern_id, occurrence_date) 쌍을 강제해
    FK 만으로는 CHECK 위반이 난다. 쓰기는 schedule_service 의 통로로만(P1).
    status 는 건드리지 않는다(confirmed 유지 = 계약 §3-2 delete_group).
    """
    from app.services.schedule_service import schedule_service

    await schedule_service.unstamp_pattern_rows(db, list(pattern_ids))


async def _materialize_for(db: AsyncSession, *, organization_id: UUID, user_id: UUID, actor: User | None) -> int:
    today = await org_today(db, organization_id)
    return await materialize_window(
        db, organization_id=organization_id, user_ids=[user_id],
        date_from=today, date_to=await window_end(db, organization_id, today),
        actor_id=actor.id if actor else None,
    )


# ─── 그룹 CRUD ──────────────────────────────────────────────────


async def create_group(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor: User,
    data: PatternGroupIn,
) -> PatternGroupOut:
    """블록 N개 → 행 N개(같은 group_id). ①④ → 400, ② → gate 없으면 409(후보 동봉),
    gate=move → 기존 그룹 start_date 만 이동(신규 생성 안 함), gate=replace → 기존 삭제 후 생성.
    저장 후 창(today..+N주) 즉시 실체화(D-g)."""
    user_id, store_id = UUID(data.user_id), UUID(data.store_id)
    await _check_blocks(db, organization_id=organization_id, user_id=user_id, data=data)

    hits = await find_overlapping_groups(
        db, organization_id=organization_id, user_id=user_id, store_id=store_id,
        blocks=data.blocks, start_date=data.start_date, until_date=data.until_date,
    )
    if hits:
        if data.gate is None:
            overlaps = [(await _group_out(db, rows)).model_dump(mode="json") for rows in hits.values()]
            raise PATTERN_OVERLAP_EXISTING(overlaps=overlaps)
        if data.gate == "move":
            today = await org_today(db, organization_id)
            moved: list[StaffWorkPattern] = []
            for gid, rows in hits.items():
                if _started(rows, today):
                    raise PATTERN_GROUP_STARTED(group_id=str(gid), start_date=min(r.start_date for r in rows).isoformat())
                for r in rows:
                    if r.until_date is not None and r.until_date < data.start_date:
                        raise PATTERN_MOVE_PAST_END(
                            group_id=str(gid), until_date=r.until_date.isoformat(),
                            start_date=data.start_date.isoformat(),
                        )
                    r.start_date = data.start_date
                moved.extend(rows)
            await db.commit()
            for gid in hits:
                await sweep_group(db, group_id=gid, actor=actor)
            await _materialize_for(db, organization_id=organization_id, user_id=user_id, actor=actor)
            first_gid = next(iter(hits))
            return await _group_out(db, await _load_group(db, first_gid, organization_id), include_ended=False)
        # replace
        for gid in list(hits):
            await delete_group(db, organization_id=organization_id, group_id=gid, actor=actor)

    group_id = uuid.uuid4()
    rows = _build_rows(organization_id=organization_id, group_id=group_id, actor=actor, data=data)
    db.add_all(rows)
    await db.commit()
    await _materialize_for(db, organization_id=organization_id, user_id=user_id, actor=actor)
    return await _group_out(db, await _load_group(db, group_id, organization_id), include_ended=False)


async def update_group(
    db: AsyncSession,
    *,
    organization_id: UUID,
    group_id: UUID,
    actor: User,
    data: PatternGroupIn,
) -> PatternGroupOut:
    """블록 전체 교체(group_id 유지). 시작 전 그룹 → 그대로 교체. 진행 중 → 옛 행 until=today-1,
    새 행 start=max(today, 입력) (§5.2). 이후 sweep_group(새 블록 기준) + materialize_window."""
    today = await org_today(db, organization_id)
    old = await _load_group(db, group_id, organization_id)
    if str(old[0].user_id) != data.user_id or str(old[0].store_id) != data.store_id:
        raise PATTERN_SUBJECT_IMMUTABLE(group_id=str(group_id))
    user_id, store_id = UUID(data.user_id), UUID(data.store_id)
    await _check_blocks(db, organization_id=organization_id, user_id=user_id, data=data)
    hits = await find_overlapping_groups(
        db, organization_id=organization_id, user_id=user_id, store_id=store_id,
        blocks=data.blocks, start_date=data.start_date, until_date=data.until_date,
        exclude_group_id=group_id,
    )
    if hits:
        overlaps = [(await _group_out(db, rows)).model_dump(mode="json") for rows in hits.values()]
        raise PATTERN_OVERLAP_EXISTING(overlaps=overlaps)

    started = _started(old, today)
    new_rows = _build_rows(
        organization_id=organization_id, group_id=group_id, actor=actor, data=data,
        floor_start=today if started else None,
    )
    db.add_all(new_rows)
    await db.commit()  # 실체화/sweep 의 건별 commit 에 휘말리지 않게 먼저 확정

    # 미래 실 행을 새 블록에 맞춘다 (도장도 새 pattern_id 로 옮긴다)
    await sweep_group(db, group_id=group_id, actor=actor, active_patterns=new_rows)

    # 옛 행 정리 — 진행 중이면 어제로 종료(이미 시작한 행만), 나머지는 삭제
    yesterday = today - timedelta(days=1)
    to_delete = [r for r in old if not (started and r.start_date <= yesterday)]
    await _unstamp_rows(db, [r.id for r in to_delete])
    for r in old:
        if r in to_delete:
            await db.delete(r)
        elif r.until_date is None or r.until_date > yesterday:
            r.until_date = yesterday
    await db.commit()

    await _materialize_for(db, organization_id=organization_id, user_id=user_id, actor=actor)
    return await _group_out(db, await _load_group(db, group_id, organization_id), include_ended=False)


async def move_group(
    db: AsyncSession,
    *,
    organization_id: UUID,
    group_id: UUID,
    actor: User,
    delta_days: int,
) -> PatternGroupOut:
    """묶음 델타 이동. 시작 전 그룹만(진행 중 → 409 PATTERN_GROUP_STARTED).
    결과 start_date < today → 409 PATTERN_MOVE_INTO_PAST."""
    today = await org_today(db, organization_id)
    rows = await _load_group(db, group_id, organization_id)
    if _started(rows, today):
        raise PATTERN_GROUP_STARTED(group_id=str(group_id), start_date=min(r.start_date for r in rows).isoformat())
    delta = timedelta(days=delta_days)
    new_start = min(r.start_date for r in rows) + delta
    if new_start < today:
        raise PATTERN_MOVE_INTO_PAST(start_date=new_start.isoformat(), delta_days=delta_days)
    for r in rows:
        r.start_date = r.start_date + delta
        if r.until_date is not None:
            r.until_date = r.until_date + delta
    await db.commit()
    await sweep_group(db, group_id=group_id, actor=actor)
    await _materialize_for(db, organization_id=organization_id, user_id=rows[0].user_id, actor=actor)
    return await _group_out(db, await _load_group(db, group_id, organization_id), include_ended=False)


async def delete_group(
    db: AsyncSession,
    *,
    organization_id: UUID,
    group_id: UUID,
    actor: User | None,
) -> None:
    """패턴 행 삭제. 기존 실 행은 FK SET NULL 로 일회성(confirmed 유지). 단 **미래 미손댐 자동생성분**
    (`overridden=False ∧ operating_day ≥ today ∧ status != deleted`)은 먼저 `delete_entry` 로 정리."""
    from app.services.schedule_service import schedule_service

    today = await org_today(db, organization_id)
    rows = await _load_group(db, group_id, organization_id)
    pattern_ids = [r.id for r in rows]
    future_ids = list((await db.execute(
        select(Schedule.id).where(
            Schedule.pattern_id.in_(pattern_ids),
            Schedule.operating_day >= today,
            Schedule.pattern_overridden.is_(False),
            Schedule.status != "deleted",
        )
    )).scalars())
    for sid in future_ids:
        await schedule_service.delete_entry(db, sid, organization_id, actor=actor)
    await _unstamp_rows(db, pattern_ids)
    await db.execute(delete(StaffWorkPattern).where(StaffWorkPattern.id.in_(pattern_ids)))
    await db.commit()


# ─── occurrence (virtual → 실 행) ────────────────────────────────


async def _pattern_or_404(db: AsyncSession, pattern_id: UUID, organization_id: UUID) -> StaffWorkPattern:
    p = await db.scalar(select(StaffWorkPattern).where(
        StaffWorkPattern.id == pattern_id, StaffWorkPattern.organization_id == organization_id,
    ))
    if p is None:
        raise PATTERN_NOT_FOUND(pattern_id=str(pattern_id))
    return p


async def _occurrence_or_400(db: AsyncSession, p: StaffWorkPattern, d: date):
    gate = await assignable_until_map(db, p.organization_id, [p.user_id])
    occs = expand([p], date_from=d, date_to=d, assignable_until=gate)
    if not occs:
        raise PATTERN_NO_OCCURRENCE(pattern_id=str(p.id), date=d.isoformat())
    return occs[0]


async def _slot_row(db: AsyncSession, pattern_id: UUID, d: date) -> Schedule | None:
    return await db.scalar(select(Schedule).where(
        Schedule.pattern_id == pattern_id, Schedule.pattern_occurrence_date == d,
    ))


async def materialize_occurrence(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor: User,
    pattern_id: UUID,
    occurrence_date: date,
    action: Literal["edit", "delete"],
    patch: ScheduleUpdate | None = None,
) -> ScheduleResponse:
    """virtual 한 칸 → 실 행. edit: 실체화 + patch + overridden=True / delete: 실체화 + soft delete + overridden=True.
    이미 실체화된 슬롯(유니크 위반)이면 그 행에 적용. deleted 슬롯은 edit 거부, delete 는 no-op."""
    from app.services.schedule_service import schedule_service

    p = await _pattern_or_404(db, pattern_id, organization_id)
    row = await _slot_row(db, pattern_id, occurrence_date)
    if row is None:
        o = await _occurrence_or_400(db, p, occurrence_date)
        try:
            with materializing(o.pattern_id, o.occurrence_date):
                resp = await schedule_service.create_entry(
                    db, organization_id, _create_payload(o), actor.id,
                    pattern_stamp=(o.pattern_id, o.occurrence_date),
                )
            row = await db.get(Schedule, UUID(resp.id))
        except IntegrityError:
            # 유니크 위반 = 동시에 누가 먼저 실체화함 → 그 행에 적용. 그래도 없으면(이론상 불가) 원 예외를 올린다.
            row = await _slot_row(db, pattern_id, occurrence_date)
            if row is None:
                raise
    assert row is not None

    if action == "edit":
        if row.status == "deleted":
            raise PATTERN_DAY_REMOVED(schedule_id=str(row.id), date=occurrence_date.isoformat())
        if patch is None:
            raise PATTERN_PATCH_REQUIRED()
        await schedule_service.update_entry(db, row.id, organization_id, patch, actor=actor)
    else:
        if row.status != "deleted":
            await schedule_service.delete_entry(db, row.id, organization_id, actor=actor)

    await schedule_service.set_pattern_overridden(db, row.id, organization_id, value=True)
    return await schedule_service.get_entry(db, row.id, organization_id)


async def revert_to_pattern(
    db: AsyncSession,
    *,
    organization_id: UUID,
    entry_id: UUID,
    actor: User | None,
) -> ScheduleResponse:
    """`pattern_overridden=True ∧ status != deleted` 만. 값을 패턴 기준으로 되돌리고 overridden=False,
    audit `reverted_to_pattern`. 아니면 409 PATTERN_REVERT_NOT_OVERRIDDEN."""
    from app.services.schedule_service import schedule_service

    # 존재 확인은 schedule_service 가 한다(404 는 그쪽 계약)
    await schedule_service.get_entry(db, entry_id, organization_id)
    entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
    assert entry is not None
    if entry.pattern_id is None or not entry.pattern_overridden or entry.status == "deleted":
        raise PATTERN_REVERT_NOT_OVERRIDDEN(schedule_id=str(entry_id), status=entry.status)
    p = await _pattern_or_404(db, entry.pattern_id, organization_id)
    o = await _occurrence_or_400(db, p, entry.pattern_occurrence_date)  # type: ignore[arg-type]
    await schedule_service.update_entry(
        db, entry_id, organization_id, patch_from_occurrence(o, reason="reverted_to_pattern"), actor=actor,
    )
    await schedule_service.set_pattern_overridden(db, entry_id, organization_id, value=False)
    await schedule_service._log_audit(
        db, entry_id, "reverted_to_pattern", actor,
        description="Reverted to fixed schedule",
    )
    await db.commit()
    return await schedule_service.get_entry(db, entry_id, organization_id)
