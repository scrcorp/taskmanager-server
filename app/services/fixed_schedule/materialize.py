"""고정 근무 — 실체화 (계약 §3-4).

materialize_window  창 안 virtual → 실 행 (`create_entry(status="confirmed", pattern_stamp=…)`)
sweep_group         그룹 패턴 변경을 **미손댐(overridden=False) 미래 실 행**에만 반영. overridden 은 절대 켜지 않는다
cleanup_future      퇴사·배정 변경 뒤 미래 자동생성분 정리

schedules 쓰기는 **`schedule_service.create_entry / update_entry / delete_entry` 만** 쓴다.
도장 컬럼(pattern_id / pattern_occurrence_date / pattern_overridden)은 이 도메인의 소유라
ORM 객체에 직접 set 한다(서비스가 그 컬럼을 모른다).

트랜잭션 메모: `create_entry` 등은 내부에서 **건별 commit/rollback** 한다. 따라서 호출자는
실체화 전에 자기 변경(패턴 행)을 먼저 commit 해야 한다 — 안 그러면 첫 실패의 rollback 이
패턴 행까지 되돌린다. cron 진입점 run_weekly_window_tick / run_daily_catchup_tick 은 파일 끝.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule
from app.models.user import User
from app.models.work_pattern import StaffWorkPattern
from app.schemas.schedule import ScheduleCreate, ScheduleUpdate
from app.services.fixed_schedule.expand import Occurrence, expand
from app.services.fixed_schedule.validation import (
    assignable_until_map,
    materializing,
)
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
from app.utils.timezone import format_naive_iso

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_WEEKS = 2
WINDOW_SETTING_KEY = "schedule.fixed_window_weeks"


async def window_weeks(db: AsyncSession, organization_id: UUID) -> int:
    """`schedule.fixed_window_weeks` 설정(없으면 2)."""
    try:
        raw = await resolve_setting(db, key=WINDOW_SETTING_KEY, organization_id=organization_id)
        if raw is not None:
            return max(1, int(raw))
    except (SettingNotRegisteredError, TypeError, ValueError):
        pass
    return DEFAULT_WINDOW_WEEKS


async def window_end(db: AsyncSession, organization_id: UUID, today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(weeks=await window_weeks(db, organization_id))


def _create_payload(o: Occurrence) -> ScheduleCreate:
    return ScheduleCreate(
        user_id=str(o.user_id),
        store_id=str(o.store_id),
        work_role_id=str(o.work_role_id) if o.work_role_id else None,
        work_date=o.occurrence_date,
        operating_day=o.occurrence_date,
        start_at=format_naive_iso(o.start_at),
        end_at=format_naive_iso(o.end_at),
        break_start_at=format_naive_iso(o.break_start_at),
        break_end_at=format_naive_iso(o.break_end_at),
        status="confirmed",
        # 경고(겹침 등)는 확인하고 넘어간다 — 실체화는 사람이 이미 결정한 패턴의 집행이다.
        # 에러(퇴사·매장 미배정·잠긴 기간)는 그대로 실패 → skip.
        force=True,
    )


async def materialize_one(
    db: AsyncSession,
    o: Occurrence,
    *,
    organization_id: UUID,
    actor_id: UUID | None,
) -> Schedule | None:
    """occurrence 1개 → 실 행. 이미 슬롯이 점유돼 있으면(유니크 위반) 그 행을 돌려준다.
    그 외 실패는 None (사유는 로그)."""
    from app.services.schedule_service import schedule_service

    try:
        with materializing(o.pattern_id, o.occurrence_date):
            resp = await schedule_service.create_entry(
                db, organization_id, _create_payload(o), actor_id,
                pattern_stamp=(o.pattern_id, o.occurrence_date),
            )
        return await db.get(Schedule, UUID(resp.id))
    except IntegrityError:
        # 이미 실체화됨 — 정상. create_entry 가 rollback 까지 끝냈다.
        return await db.scalar(select(Schedule).where(
            Schedule.pattern_id == o.pattern_id,
            Schedule.pattern_occurrence_date == o.occurrence_date,
        ))
    except Exception as exc:  # noqa: BLE001 — 한 건 실패가 창 전체를 멈추면 안 된다
        logger.warning(
            "fixed_schedule: skip materialize pattern=%s date=%s: %s",
            o.pattern_id, o.occurrence_date, exc,
        )
        return None


async def materialize_window(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_ids: Sequence[UUID] | None = None,
    date_from: date,
    date_to: date,
    actor_id: UUID | None = None,
) -> int:
    """창 안 남은 virtual 을 전부 실 행으로. 생성 건수 반환. 멱등(두 번 돌리면 0)."""
    from app.services.fixed_schedule.read import virtual_occurrences

    occs = await virtual_occurrences(
        db, organization_id=organization_id, date_from=date_from, date_to=date_to, user_ids=user_ids,
    )
    created = 0
    for o in occs:
        row = await materialize_one(db, o, organization_id=organization_id, actor_id=actor_id)
        if row is not None:
            created += 1
    return created


# ─── sweep ───────────────────────────────────────────────────────


def _occ_matches_row(o: Occurrence, row: Schedule) -> bool:
    return (
        row.start_at == o.start_at
        and row.end_at == o.end_at
        and row.break_start_at == o.break_start_at
        and row.break_end_at == o.break_end_at
        and row.work_role_id == o.work_role_id
        and row.operating_day == o.occurrence_date
    )


def patch_from_occurrence(o: Occurrence, *, reason: str) -> ScheduleUpdate:
    """occurrence 값 그대로의 ScheduleUpdate (sweep / revert 공용)."""
    return ScheduleUpdate(
        operating_day=o.occurrence_date,
        start_at=format_naive_iso(o.start_at),
        end_at=format_naive_iso(o.end_at),
        break_start_at=format_naive_iso(o.break_start_at),
        break_end_at=format_naive_iso(o.break_end_at),
        # "" → update_entry 가 None(역할 해제)으로 해석한다. None 은 "미지정" 이라 쓸 수 없다.
        work_role_id=str(o.work_role_id) if o.work_role_id else "",
        force=True,
        change_reason=reason,
    )


def _sweep_patch(o: Occurrence) -> ScheduleUpdate:
    return patch_from_occurrence(o, reason="pattern_swept")


async def sweep_group(
    db: AsyncSession,
    *,
    group_id: UUID,
    date_from: date | None = None,
    actor: User | None = None,
    active_patterns: Sequence[StaffWorkPattern] | None = None,
) -> tuple[int, int]:
    """그룹의 **미손댐 미래 실 행**을 현재 패턴값으로 다시 맞춘다 → (updated, skipped).

    대상: pattern_id ∈ group ∧ operating_day ≥ today ∧ status != 'deleted' ∧ overridden = False.
    - 같은 날 occurrence 가 있으면 시간/역할/날짜를 `update_entry` (reason pattern_swept)
    - 없으면(요일이 빠짐·기간 밖) `delete_entry`
    - 그룹 블록이 교체돼 pattern_id 가 바뀌었으면 같은 날 새 occurrence 로 **도장만 옮긴다**
      (overridden 행도 도장은 옮긴다 — 값은 건드리지 않는다 → skipped 로 센다)
    **overridden 은 절대 켜지 않는다.**
    active_patterns: "기대값" 을 계산할 패턴을 명시(update_group 이 새 블록만 넘긴다). 없으면 그룹 전체.
    대상 실 행은 항상 그룹의 **모든** 패턴 id(옛 블록 포함)로 찾는다.
    """
    from app.services.schedule_service import schedule_service

    today = date_from or date.today()
    patterns = list((await db.execute(
        select(StaffWorkPattern).where(StaffWorkPattern.group_id == group_id)
    )).scalars())
    if not patterns:
        return (0, 0)
    org_id = patterns[0].organization_id
    user_id = patterns[0].user_id

    rows = list((await db.execute(
        select(Schedule).where(
            Schedule.pattern_id.in_([p.id for p in patterns]),
            Schedule.operating_day >= today,
            Schedule.status != "deleted",
        ).order_by(Schedule.operating_day)
    )).scalars())
    if not rows:
        return (0, 0)

    horizon = max(max(r.operating_day for r in rows), max(
        (r.pattern_occurrence_date for r in rows if r.pattern_occurrence_date), default=today
    ))
    gate = await assignable_until_map(db, org_id, [user_id])
    expected = expand(
        list(active_patterns) if active_patterns is not None else patterns,
        date_from=today, date_to=horizon, assignable_until=gate,
    )
    by_date: dict[date, list[Occurrence]] = {}
    for o in expected:
        by_date.setdefault(o.occurrence_date, []).append(o)
    claimed: set[tuple[UUID, date]] = set()

    def _pick(row: Schedule) -> Occurrence | None:
        cands = by_date.get(row.pattern_occurrence_date or row.operating_day, [])
        same = [o for o in cands if o.pattern_id == row.pattern_id and (o.pattern_id, o.occurrence_date) not in claimed]
        pool = same or [o for o in cands if (o.pattern_id, o.occurrence_date) not in claimed]
        if not pool:
            return None
        claimed.add((pool[0].pattern_id, pool[0].occurrence_date))
        return pool[0]

    updated = skipped = 0
    for row in rows:
        o = _pick(row)
        try:
            if row.pattern_overridden:
                # 사람이 손댄 행 — 값은 보존. 도장만 새 패턴으로 옮길 수 있으면 옮긴다.
                if o is not None and o.pattern_id != row.pattern_id:
                    await schedule_service.set_pattern_stamp(
                        db, row.id, org_id, stamp=(o.pattern_id, o.occurrence_date),
                    )
                skipped += 1
                continue
            if o is None:
                await schedule_service.delete_entry(db, row.id, org_id, actor=actor)
                updated += 1
                continue
            restamp = o.pattern_id != row.pattern_id
            if not _occ_matches_row(o, row):
                await schedule_service.update_entry(db, row.id, org_id, _sweep_patch(o), actor=actor)
                updated += 1
            if restamp:
                await schedule_service.set_pattern_stamp(
                    db, row.id, org_id, stamp=(o.pattern_id, o.occurrence_date),
                )
        except Exception as exc:  # noqa: BLE001 — 잠긴 기간 등 한 건 실패는 건너뛴다
            logger.warning("fixed_schedule: sweep skip schedule=%s: %s", row.id, exc)
            skipped += 1
    return (updated, skipped)


async def cleanup_future(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    after: date,
    actor: User | None = None,
) -> int:
    """`operating_day > after ∧ pattern_id NOT NULL ∧ overridden=False ∧ status='confirmed'` → delete_entry.
    퇴사예정일·매장 배정 변경 뒤 호출한다 (훅 지점은 server-api-cron 몫)."""
    from app.services.schedule_service import schedule_service

    ids = list((await db.execute(
        select(Schedule.id).where(
            Schedule.organization_id == organization_id,
            Schedule.user_id == user_id,
            Schedule.operating_day > after,
            Schedule.pattern_id.is_not(None),
            Schedule.pattern_overridden.is_(False),
            Schedule.status == "confirmed",
        )
    )).scalars())
    done = 0
    for sid in ids:
        try:
            await schedule_service.delete_entry(db, sid, organization_id, actor=actor)
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("fixed_schedule: cleanup skip schedule=%s: %s", sid, exc)
    return done


# ─── cron 진입점 (계약 §3-4 run_weekly_window_tick / run_daily_catchup_tick) ──
#
# 스케줄러는 매시 정각(UTC)에 깨우고, "지금이 자기 실행 시각인 org" 만 처리한다 —
# push_digest_service.run_digest_tick 과 같은 패턴. org 마다 tz 가 다르므로 요일·시각·오늘
# 판단은 전부 org 로컬로 한다.
#   weekly  : 일요일(주 시작, 0=Sun) FIXED_TICK_HOUR 시 — 창(today..+N주)을 한 주 밀어 채운다
#   daily   : 매일 FIXED_TICK_HOUR 시 — 같은 범위를 다시 채운다(멱등이라 평소 0건, 놓친 이벤트 보정)
# 두 잡 모두 건별 알림을 내지 않는다(D-e). 예외는 삼킨다 — 잡이 죽으면 다음 tick 이 안 돈다.

FIXED_TICK_HOUR = 3
FIXED_WEEKLY_DOW = 0  # 0=Sun .. 6=Sat (일요일 시작)


def _org_local_now(tz_name: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — 잘못된 tz 문자열이 잡을 막으면 안 된다
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


async def _run_window_tick(
    *,
    label: str,
    require_dow: int | None,
    now_hour: int | None,
    now_dow: int | None,
    today: date | None,
) -> int:
    """org 순회 → 로컬 시각(+요일) 이 맞는 org 만 `materialize_window(today..window_end)`.
    now_hour / now_dow / today 는 테스트 주입용(None 이면 org 로컬 현재값). 생성 건수 합을 돌려준다."""
    from sqlalchemy import select as _select

    from app.database import async_session
    from app.models.organization import Organization
    from app.services.fixed_schedule.expand import dow_sun0

    total = 0
    try:
        async with async_session() as db:
            orgs = (await db.execute(
                _select(Organization).where(Organization.deleted_at.is_(None))
            )).scalars().all()
            for org in orgs:
                local = _org_local_now(org.timezone)
                hour = now_hour if now_hour is not None else local.hour
                if hour != FIXED_TICK_HOUR:
                    continue
                if require_dow is not None:
                    dow = now_dow if now_dow is not None else dow_sun0(local.date())
                    if dow != require_dow:
                        continue
                org_today = today or local.date()
                try:
                    created = await materialize_window(
                        db, organization_id=org.id,
                        date_from=org_today, date_to=await window_end(db, org.id, org_today),
                    )
                except Exception as exc:  # noqa: BLE001 — 한 org 실패가 다른 org 를 막으면 안 된다
                    logger.exception("[fixed_schedule:%s] org=%s failed: %s", label, org.id, exc)
                    await db.rollback()
                    continue
                if created:
                    logger.info("[fixed_schedule:%s] org=%s materialized %d", label, org.id, created)
                total += created
    except Exception as exc:  # noqa: BLE001
        logger.exception("[fixed_schedule:%s] tick failed: %s", label, exc)
    return total


async def run_weekly_window_tick(
    now_hour: int | None = None, now_dow: int | None = None, today: date | None = None,
) -> int:
    """주 1회(org 로컬 일요일 FIXED_TICK_HOUR 시) 창 밀기. 멱등."""
    return await _run_window_tick(
        label="weekly", require_dow=FIXED_WEEKLY_DOW, now_hour=now_hour, now_dow=now_dow, today=today,
    )


async def run_daily_catchup_tick(now_hour: int | None = None, today: date | None = None) -> int:
    """일 1회(org 로컬 FIXED_TICK_HOUR 시) 같은 범위 catch-up. 멱등이라 평소 0건."""
    return await _run_window_tick(
        label="daily_catchup", require_dow=None, now_hour=now_hour, now_dow=None, today=today,
    )
