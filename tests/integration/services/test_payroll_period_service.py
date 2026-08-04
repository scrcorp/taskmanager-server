"""Integration tests — payroll_period_service (Payroll v1 Phase 2).

검증 대상 (스펙 §4 / 설계방향 C4·C6 / 계산 규칙 4):
    - ensure_period: get-or-create 멱등, org_id 는 store 에서 유도, status='open',
      uq(store, start) 로 중복 원천 차단, 없는 store 는 NotFoundError
    - get_period / list_periods (겹침 범위 조회, start 오름차순)
    - is_date_locked: confirmed 기간 안만 True (경계 포함), open/무기간/타매장 False
    - card_tips_for_period: own card − 나간 분배 전액 + 받은 분배(수락분만),
      기간 밖/타매장 entry 제외, cash 미포함 — tip 도메인 공식과 동일 원천
    - tip_period_status_for: 같은 (store, 범위) tip_period 의 status, 없으면 None
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
from app.models.organization import Store
from app.models.payroll import PayPeriod
from app.models.tip import TipDistribution, TipEntry, TipPeriod
from app.models.user import User
from app.services.payroll_period_service import payroll_period_service
from app.utils.exceptions import NotFoundError


# ---------------------------------------------------------------------------
# 픽스처 — throwaway store 2개 + 직원 3명 (공용 시드 오염 방지, 종료 시 정리)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def payroll_period_env(
    seed_organization: dict, seed_roles: dict[str, UUID],
) -> AsyncIterator[dict]:
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        store = Store(
            organization_id=org_id,
            name=f"__pp_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        other_store = Store(
            organization_id=org_id,
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
        db.add_all([store, other_store, *users])
        await db.commit()
        for obj in (store, other_store, *users):
            await db.refresh(obj)
        env = {
            "org_id": org_id,
            "store_id": store.id,
            "other_store_id": other_store.id,
            "alice": users[0].id,
            "bob": users[1].id,
            "carol": users[2].id,
        }

    yield env

    async with async_session() as db:
        store_ids = [env["store_id"], env["other_store_id"]]
        # dists 는 entry CASCADE 로 함께 삭제
        await db.execute(delete(TipEntry).where(TipEntry.store_id.in_(store_ids)))
        await db.execute(delete(TipPeriod).where(TipPeriod.store_id.in_(store_ids)))
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id.in_(store_ids)))
        await db.execute(delete(Store).where(Store.id.in_(store_ids)))
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
    """첫 호출: 반월 경계로 생성, org 는 store 에서 유도, status='open'."""
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=payroll_period_env["store_id"], date_in_period=date(2026, 8, 20),
        )
        await db.commit()

        assert period.start_date == date(2026, 8, 16)
        assert period.end_date == date(2026, 8, 31)
        assert period.status == "open"
        assert period.organization_id == payroll_period_env["org_id"]


async def test_ensure_period_idempotent(payroll_period_env: dict) -> None:
    """같은 반월 안 어떤 날짜로 다시 불러도 같은 행 — 새 행 안 생김."""
    store_id = payroll_period_env["store_id"]
    async with async_session() as db:
        first = await payroll_period_service.ensure_period(
            db, store_id=store_id, date_in_period=date(2026, 8, 1),
        )
        await db.commit()
        first_id = first.id

    async with async_session() as db:
        again = await payroll_period_service.ensure_period(
            db, store_id=store_id, date_in_period=date(2026, 8, 15),
        )
        assert again.id == first_id

        count = len((await db.scalars(
            select(PayPeriod).where(PayPeriod.store_id == store_id)
        )).all())
        assert count == 1


async def test_ensure_period_unknown_store(payroll_period_env: dict) -> None:
    async with async_session() as db:
        with pytest.raises(NotFoundError):
            await payroll_period_service.ensure_period(
                db, store_id=uuid_mod.uuid4(), date_in_period=date(2026, 8, 1),
            )


async def test_get_period_none_before_create(payroll_period_env: dict) -> None:
    """get_period 는 자동 생성하지 않는다."""
    async with async_session() as db:
        found = await payroll_period_service.get_period(
            db, store_id=payroll_period_env["store_id"], date_in_period=date(2026, 8, 1),
        )
        assert found is None


async def test_list_periods_overlap_and_order(payroll_period_env: dict) -> None:
    """겹침 조회: 범위에 걸친 기간만, start 오름차순. 타매장 제외."""
    store_id = payroll_period_env["store_id"]
    async with async_session() as db:
        for d in (date(2026, 7, 20), date(2026, 8, 1), date(2026, 8, 16)):
            await payroll_period_service.ensure_period(
                db, store_id=store_id, date_in_period=d,
            )
        # 타매장 기간 — 결과에 나오면 안 됨
        await payroll_period_service.ensure_period(
            db, store_id=payroll_period_env["other_store_id"],
            date_in_period=date(2026, 8, 1),
        )
        await db.commit()

    async with async_session() as db:
        # 7/31~8/15 → 7월 후반(끝 7/31)과 8월 전반이 겹침, 8월 후반은 제외
        periods = await payroll_period_service.list_periods(
            db, store_id=store_id,
            range_start=date(2026, 7, 31), range_end=date(2026, 8, 15),
        )
        assert [(p.start_date, p.end_date) for p in periods] == [
            (date(2026, 7, 16), date(2026, 7, 31)),
            (date(2026, 8, 1), date(2026, 8, 15)),
        ]
        assert all(p.store_id == store_id for p in periods)


# ---------------------------------------------------------------------------
# is_date_locked
# ---------------------------------------------------------------------------


async def test_is_date_locked_branches(payroll_period_env: dict) -> None:
    """무기간/open → False. confirmed → 경계 포함 True, 밖/타매장 False."""
    store_id = payroll_period_env["store_id"]

    async def locked(db, d: date, sid: UUID = store_id) -> bool:
        return await payroll_period_service.is_date_locked(
            db, store_id=sid, work_date=d,
        )

    async with async_session() as db:
        # 기간 자체가 없음
        assert await locked(db, date(2026, 8, 3)) is False

        period = await payroll_period_service.ensure_period(
            db, store_id=store_id, date_in_period=date(2026, 8, 3),
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
        # 타매장은 잠금 없음
        assert await locked(
            db, date(2026, 8, 3), payroll_period_env["other_store_id"],
        ) is False


# ---------------------------------------------------------------------------
# card_tips_for_period / tip_period_status_for
# ---------------------------------------------------------------------------


async def test_card_tips_for_period_distribution_directions(payroll_period_env: dict) -> None:
    """own − 나간 전액(pending 포함) + 받은 수락분(accepted/auto_accepted)만.

    alice: card 100, cash 20 — bob 에게 30 accepted, carol 에게 10 pending
    bob:   card 50 — carol 에게 5 auto_accepted
    carol: entry 없음 — 수락된 수취만 있음
    기대: alice 100−40=60 / bob 50−5+30=75 / carol 0+5=5 (pending 10 미포함)
    """
    env = payroll_period_env
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=env["store_id"], date_in_period=date(2026, 8, 3),
        )
        e_alice = await _add_entry(
            db, env, "alice", day=date(2026, 8, 3), card="100.00", cash="20.00",
        )
        e_bob = await _add_entry(db, env, "bob", day=date(2026, 8, 5), card="50.00")
        _add_dist(db, e_alice, env["bob"], "30.00", "accepted")
        _add_dist(db, e_alice, env["carol"], "10.00", "pending")
        _add_dist(db, e_bob, env["carol"], "5.00", "auto_accepted")

        # 기간 밖(8/16) / 타매장 entry — 집계 제외 확인용
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
            db, store_id=env["store_id"], period=period,
        )

        assert tips[env["alice"]] == Decimal("60.00")   # cash 20 미포함
        assert tips[env["bob"]] == Decimal("75.00")
        assert tips[env["carol"]] == Decimal("5.00")    # pending 10 미포함
        assert set(tips.keys()) == {env["alice"], env["bob"], env["carol"]}


async def test_card_tips_for_period_empty(payroll_period_env: dict) -> None:
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=payroll_period_env["store_id"], date_in_period=date(2026, 8, 3),
        )
        tips = await payroll_period_service.card_tips_for_period(
            db, store_id=payroll_period_env["store_id"], period=period,
        )
        assert tips == {}


async def test_tip_period_status_for(payroll_period_env: dict) -> None:
    """같은 (store, 범위) tip_period 의 status 미러 — 없으면 None (계산 규칙 4 입력)."""
    env = payroll_period_env
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=env["store_id"], date_in_period=date(2026, 8, 3),
        )
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        period = (await db.scalars(
            select(PayPeriod).where(PayPeriod.id == period_id)
        )).one()

        # tip_period 없음 → None
        assert await payroll_period_service.tip_period_status_for(
            db, store_id=env["store_id"], period=period,
        ) is None

        tp = TipPeriod(
            store_id=env["store_id"],
            start_date=period.start_date,
            end_date=period.end_date,
            status="open",
        )
        db.add(tp)
        await db.flush()
        assert await payroll_period_service.tip_period_status_for(
            db, store_id=env["store_id"], period=period,
        ) == "open"

        tp.status = "confirmed"
        await db.flush()
        assert await payroll_period_service.tip_period_status_for(
            db, store_id=env["store_id"], period=period,
        ) == "confirmed"
        await db.commit()
