"""Integration tests — payroll_period_service (Payroll v1 Phase 2 + group 스코프).

검증 대상 (스펙 §4 / 설계방향 C4·C6 / 계산 규칙 4 / 2026-08-19 D1~D5):
    - ensure_period: get-or-create 멱등, org_id 는 group 에서 유도, status='open',
      uq(group, start) 로 중복 원천 차단, 없는 group 은 NotFoundError
    - get_period / list_periods (겹침 범위 조회, start 오름차순, 레거시 포함)
    - is_date_locked: 호출은 store 키 그대로 — group 확정이면 **그룹 내 전 매장**
      True (D3), 타그룹 False, 레거시 store 확정 행도 잠금 유지
    - card_tips_for_period: own card − 나간 분배 전액 + 받은 분배(수락분),
      기간 밖/타그룹 entry 제외, cash 미포함, 그룹 내 매장 합산
    - tip_period_status_for: 매장별 status map + aggregate_tip_status 요약
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.organization import Store, StoreGroup
from app.models.payroll import PayPeriod
from app.models.tip import TipDistribution, TipEntry, TipPeriod
from app.models.user import User
from app.services.payroll_period_service import payroll_period_service
from app.utils.exceptions import AppError


# ---------------------------------------------------------------------------
# 픽스처 — 그룹 2개(본그룹은 매장 2곳) + 직원 3명 (종료 시 정리)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def payroll_period_env(
    seed_organization: dict, seed_roles: dict[str, UUID],
) -> AsyncIterator[dict]:
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        group = StoreGroup(organization_id=org_id, name=f"__pp_group_{suffix}")
        other_group = StoreGroup(
            organization_id=org_id, name=f"__pp_other_group_{suffix}"
        )
        db.add_all([group, other_group])
        await db.flush()
        store = Store(
            organization_id=org_id,
            group_id=group.id,
            name=f"__pp_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        # 같은 그룹의 2호점 — 그룹 잠금/합산 검증용
        sibling_store = Store(
            organization_id=org_id,
            group_id=group.id,
            name=f"__pp_sibling_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        other_store = Store(
            organization_id=org_id,
            group_id=other_group.id,
            name=f"__pp_other_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        users = [
            User(
                organization_id=org_id,
                role_id=seed_roles["staff"],
                username=f"__pp_{name}_{suffix}",
                full_name=f"PP {name.upper()}",
                password_hash="x",
                is_active=True,
            )
            for name in ("alice", "bob", "carol")
        ]
        db.add_all([store, sibling_store, other_store, *users])
        await db.commit()
        for obj in (group, other_group, store, sibling_store, other_store, *users):
            await db.refresh(obj)
        env = {
            "org_id": org_id,
            "group_id": group.id,
            "other_group_id": other_group.id,
            "store_id": store.id,
            "sibling_store_id": sibling_store.id,
            "other_store_id": other_store.id,
            "alice": users[0].id,
            "bob": users[1].id,
            "carol": users[2].id,
        }

    yield env

    async with async_session() as db:
        store_ids = [env["store_id"], env["sibling_store_id"], env["other_store_id"]]
        group_ids = [env["group_id"], env["other_group_id"]]
        # dists 는 entry CASCADE 로 함께 삭제
        await db.execute(delete(TipEntry).where(TipEntry.store_id.in_(store_ids)))
        await db.execute(delete(TipPeriod).where(TipPeriod.store_id.in_(store_ids)))
        await db.execute(
            delete(PayPeriod).where(PayPeriod.store_group_id.in_(group_ids))
        )
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id.in_(store_ids)))
        await db.execute(delete(Store).where(Store.id.in_(store_ids)))
        await db.execute(delete(StoreGroup).where(StoreGroup.id.in_(group_ids)))
        await db.execute(
            delete(User).where(User.id.in_([env["alice"], env["bob"], env["carol"]]))
        )
        await db.commit()


async def _add_entry(
    db, env: dict, employee_key: str, *, day: date, card: str, cash: str = "0",
    store_key: str = "store_id",
) -> TipEntry:
    entry = TipEntry(
        store_id=env[store_key],
        employee_id=env[employee_key],
        date=day,
        card_tips=Decimal(card),
        cash_tips_kept=Decimal(cash),
    )
    db.add(entry)
    await db.flush()
    return entry


def _add_dist(db, entry: TipEntry, receiver_id: UUID, amount: str, status: str) -> None:
    db.add(TipDistribution(
        entry_id=entry.id,
        receiver_id=receiver_id,
        amount=Decimal(amount),
        status=status,
        pending_until=datetime(2026, 8, 20, tzinfo=timezone.utc),
    ))


# ---------------------------------------------------------------------------
# ensure_period / get_period / list_periods
# ---------------------------------------------------------------------------


async def test_ensure_period_creates_with_canonical_bounds(payroll_period_env: dict) -> None:
    """첫 호출: 반월 경계로 생성, org 는 group 에서 유도, status='open'."""
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=payroll_period_env["group_id"],
            date_in_period=date(2026, 8, 20),
        )
        await db.commit()

        assert period.start_date == date(2026, 8, 16)
        assert period.end_date == date(2026, 8, 31)
        assert period.status == "open"
        assert period.organization_id == payroll_period_env["org_id"]
        assert period.store_id is None  # group 스코프 — 매장 귀속 없음


async def test_ensure_period_idempotent(payroll_period_env: dict) -> None:
    """같은 반월 안 어떤 날짜로 다시 불러도 같은 행 — 새 행 안 생김."""
    group_id = payroll_period_env["group_id"]
    async with async_session() as db:
        first = await payroll_period_service.ensure_period(
            db, store_group_id=group_id, date_in_period=date(2026, 8, 1),
        )
        await db.commit()
        first_id = first.id

    async with async_session() as db:
        again = await payroll_period_service.ensure_period(
            db, store_group_id=group_id, date_in_period=date(2026, 8, 15),
        )
        assert again.id == first_id

        count = len((await db.scalars(
            select(PayPeriod).where(PayPeriod.store_group_id == group_id)
        )).all())
        assert count == 1


async def test_ensure_period_unknown_group(payroll_period_env: dict) -> None:
    async with async_session() as db:
        with pytest.raises(AppError) as exc:
            await payroll_period_service.ensure_period(
                db, store_group_id=uuid_mod.uuid4(), date_in_period=date(2026, 8, 1),
            )
        assert exc.value.status_code == 404


async def test_get_period_none_before_create(payroll_period_env: dict) -> None:
    """get_period 는 자동 생성하지 않는다."""
    async with async_session() as db:
        found = await payroll_period_service.get_period(
            db, store_group_id=payroll_period_env["group_id"],
            date_in_period=date(2026, 8, 1),
        )
        assert found is None


async def test_list_periods_overlap_and_order(payroll_period_env: dict) -> None:
    """겹침 조회: 범위에 걸친 기간만, start 오름차순. 타그룹 제외.

    그룹 소속 매장의 레거시(store 스코프) 확정 기간도 목록에 포함된다.
    """
    env = payroll_period_env
    group_id = env["group_id"]
    async with async_session() as db:
        for d in (date(2026, 8, 1), date(2026, 8, 16)):
            await payroll_period_service.ensure_period(
                db, store_group_id=group_id, date_in_period=d,
            )
        # 레거시 store 스코프 확정 행 (전환 전 원장) — 목록에 나와야 함
        db.add(PayPeriod(
            organization_id=env["org_id"],
            store_id=env["store_id"],
            start_date=date(2026, 7, 16),
            end_date=date(2026, 7, 31),
            status="confirmed",
            confirmed_at=datetime.now(timezone.utc),
        ))
        # 타그룹 기간 — 결과에 나오면 안 됨
        await payroll_period_service.ensure_period(
            db, store_group_id=env["other_group_id"],
            date_in_period=date(2026, 8, 1),
        )
        await db.commit()

    async with async_session() as db:
        # 7/31~8/15 → 7월 후반(레거시)과 8월 전반이 겹침, 8월 후반은 제외
        periods = await payroll_period_service.list_periods(
            db, store_group_id=group_id,
            range_start=date(2026, 7, 31), range_end=date(2026, 8, 15),
        )
        assert [(p.start_date, p.end_date) for p in periods] == [
            (date(2026, 7, 16), date(2026, 7, 31)),
            (date(2026, 8, 1), date(2026, 8, 15)),
        ]
        assert periods[0].store_id == env["store_id"]  # 레거시
        assert periods[1].store_group_id == group_id


# ---------------------------------------------------------------------------
# is_date_locked
# ---------------------------------------------------------------------------


async def test_is_date_locked_branches(payroll_period_env: dict) -> None:
    """무기간/open → False. group confirmed → 그룹 내 전 매장 True (경계 포함),
    밖/타그룹 False. 레거시 store 확정 행도 잠금 유지."""
    env = payroll_period_env
    store_id = env["store_id"]

    async def locked(db, d: date, sid: UUID = store_id) -> bool:
        return await payroll_period_service.is_date_locked(
            db, store_id=sid, work_date=d,
        )

    async with async_session() as db:
        # 기간 자체가 없음
        assert await locked(db, date(2026, 8, 3)) is False

        period = await payroll_period_service.ensure_period(
            db, store_group_id=env["group_id"], date_in_period=date(2026, 8, 3),
        )
        await db.commit()

    async with async_session() as db:
        # open 기간 — 잠금 아님
        assert await locked(db, date(2026, 8, 3)) is False

        fetched = (await db.scalars(
            select(PayPeriod).where(PayPeriod.id == period.id)
        )).one()
        fetched.status = "confirmed"
        fetched.confirmed_at = datetime.now(timezone.utc)
        await db.commit()

    async with async_session() as db:
        assert await locked(db, date(2026, 8, 3)) is True
        # 경계 포함
        assert await locked(db, date(2026, 8, 1)) is True
        assert await locked(db, date(2026, 8, 15)) is True
        # 기간 밖
        assert await locked(db, date(2026, 7, 31)) is False
        assert await locked(db, date(2026, 8, 16)) is False
        # 그룹 확정 = 그룹 내 **다른 매장도** 잠금 (D3 — 법인 원장 동결)
        assert await locked(db, date(2026, 8, 3), env["sibling_store_id"]) is True
        # 타그룹 매장은 잠금 없음
        assert await locked(db, date(2026, 8, 3), env["other_store_id"]) is False

    # 레거시 store 스코프 확정 행 — 그 매장만 잠금
    async with async_session() as db:
        db.add(PayPeriod(
            organization_id=env["org_id"],
            store_id=env["other_store_id"],
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 15),
            status="confirmed",
            confirmed_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    async with async_session() as db:
        assert await locked(db, date(2026, 7, 3), env["other_store_id"]) is True
        assert await locked(db, date(2026, 7, 3)) is False


# ---------------------------------------------------------------------------
# card_tips_for_period / tip_period_status_for
# ---------------------------------------------------------------------------


async def test_card_tips_for_period_distribution_directions(payroll_period_env: dict) -> None:
    """own − 나간 전액(pending 포함) + 받은 수락분(accepted/auto_accepted)만.

    alice: card 100, cash 20 — bob 에게 30 accepted, carol 에게 10 pending
    bob:   card 50 — carol 에게 5 auto_accepted (같은 그룹 2호점)
    carol: entry 없음 — 수락된 수취만 있음
    기대: alice 100−40=60 / bob 50−5+30=75 / carol 0+5=5 (pending 10 미포함).
    2호점 entry 도 그룹 합산에 포함 — 타그룹/기간 밖은 제외.
    """
    env = payroll_period_env
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=env["group_id"], date_in_period=date(2026, 8, 3),
        )
        e_alice = await _add_entry(
            db, env, "alice", day=date(2026, 8, 3), card="100.00", cash="20.00",
        )
        # bob 의 entry 는 같은 그룹 2호점 — 그룹 합산 검증
        e_bob = await _add_entry(
            db, env, "bob", day=date(2026, 8, 5), card="50.00",
            store_key="sibling_store_id",
        )
        _add_dist(db, e_alice, env["bob"], "30.00", "accepted")
        _add_dist(db, e_alice, env["carol"], "10.00", "pending")
        _add_dist(db, e_bob, env["carol"], "5.00", "auto_accepted")

        # 기간 밖(8/16) / 타그룹 entry — 집계 제외 확인용
        await _add_entry(db, env, "alice", day=date(2026, 8, 16), card="999.00")
        await _add_entry(
            db, env, "alice", day=date(2026, 8, 3), card="777.00",
            store_key="other_store_id",
        )
        await db.commit()

    async with async_session() as db:
        period = (await db.scalars(
            select(PayPeriod).where(PayPeriod.id == period.id)
        )).one()
        tips = await payroll_period_service.card_tips_for_period(
            db, store_ids=[env["store_id"], env["sibling_store_id"]], period=period,
        )

        assert tips[env["alice"]] == Decimal("60.00")   # cash 20 미포함
        assert tips[env["bob"]] == Decimal("75.00")
        assert tips[env["carol"]] == Decimal("5.00")    # pending 10 미포함
        assert set(tips.keys()) == {env["alice"], env["bob"], env["carol"]}


async def test_card_tips_for_period_empty(payroll_period_env: dict) -> None:
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=payroll_period_env["group_id"],
            date_in_period=date(2026, 8, 3),
        )
        tips = await payroll_period_service.card_tips_for_period(
            db, store_ids=[payroll_period_env["store_id"]], period=period,
        )
        assert tips == {}


async def test_tip_period_status_for(payroll_period_env: dict) -> None:
    """매장별 status map + aggregate 요약 (계산 규칙 4 입력).

    그룹 내 전 매장이 confirmed 여야 aggregate 가 'confirmed'.
    """
    env = payroll_period_env
    store_ids = [env["store_id"], env["sibling_store_id"]]
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=env["group_id"], date_in_period=date(2026, 8, 3),
        )
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        period = (await db.scalars(
            select(PayPeriod).where(PayPeriod.id == period_id)
        )).one()

        # tip_period 없음 → 전 매장 None
        statuses = await payroll_period_service.tip_period_status_for(
            db, store_ids=store_ids, period=period,
        )
        assert statuses == {env["store_id"]: None, env["sibling_store_id"]: None}
        assert payroll_period_service.aggregate_tip_status(statuses) is None

        tp1 = TipPeriod(
            store_id=env["store_id"],
            start_date=period.start_date,
            end_date=period.end_date,
            status="open",
        )
        tp2 = TipPeriod(
            store_id=env["sibling_store_id"],
            start_date=period.start_date,
            end_date=period.end_date,
            status="confirmed",
        )
        db.add_all([tp1, tp2])
        await db.flush()
        statuses = await payroll_period_service.tip_period_status_for(
            db, store_ids=store_ids, period=period,
        )
        assert statuses[env["store_id"]] == "open"
        assert statuses[env["sibling_store_id"]] == "confirmed"
        assert payroll_period_service.aggregate_tip_status(statuses) == "open"

        tp1.status = "confirmed"
        await db.flush()
        statuses = await payroll_period_service.tip_period_status_for(
            db, store_ids=store_ids, period=period,
        )
        assert payroll_period_service.aggregate_tip_status(statuses) == "confirmed"
        await db.commit()
