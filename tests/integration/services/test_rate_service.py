"""Integration — rate_service (Payroll v1 Phase 1 핵심).

대상: app/services/rate_service.py
    - rate_at 3단 resolver (이력 우선순위 + 날짜 경계 포함)
    - record_rate_change (즉시/미래 적용, 같은날 재등록, old_rate 체인, 검증)
    - apply_due_rate_changes (도래 반영 + 멱등)

스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §1
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.organization import Organization, Store
from app.models.rate import HourlyRateHistory
from app.models.schedule import Schedule
from app.models.user import User
from app.services.rate_service import rate_service
from app.utils.exceptions import BadRequestError, NotFoundError

pytestmark = pytest.mark.asyncio


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# 픽스처 — 전용 user + org_member + store (org default 는 임시 15.00 로 고정)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def rate_ctx(seed_organization: dict, seed_roles: dict[str, UUID]) -> AsyncIterator[dict]:
    """rate 테스트 전용 컨텍스트. 종료 시 이력→스케줄→멤버→유저→스토어 정리 + org default 원복."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        org = await db.get(Organization, org_id)
        original_org_rate = org.default_hourly_rate
        org.default_hourly_rate = Decimal("15.00")

        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__rate_svc_{suffix}",
            full_name="Rate Svc Test",
            password_hash="x",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        member = OrgMember(
            user_id=user.id, organization_id=org_id, role_id=seed_roles["staff"]
        )
        store = Store(
            organization_id=org_id,
            name=f"__rate_svc_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
            default_hourly_rate=Decimal("17.25"),
        )
        db.add_all([member, store])
        await db.commit()
        await db.refresh(member)
        await db.refresh(store)
        ctx = {
            "org_id": org_id,
            "user_id": user.id,
            "member_id": member.id,
            "store_id": store.id,
        }

    yield ctx

    async with async_session() as db:
        await db.execute(delete(Schedule).where(Schedule.user_id == ctx["user_id"]))
        await db.execute(
            delete(HourlyRateHistory).where(
                HourlyRateHistory.org_member_id == ctx["member_id"]
            )
        )
        await db.execute(delete(OrgMember).where(OrgMember.id == ctx["member_id"]))
        await db.execute(delete(User).where(User.id == ctx["user_id"]))
        await db.execute(delete(Store).where(Store.id == ctx["store_id"]))
        org = await db.get(Organization, org_id)
        org.default_hourly_rate = original_org_rate
        await db.commit()


async def _set_member_rate(member_id: UUID, rate: Decimal | None) -> None:
    async with async_session() as db:
        member = await db.get(OrgMember, member_id)
        member.hourly_rate = rate
        await db.commit()


async def _add_history(ctx: dict, *, new_rate: str, effective: date, old_rate: str | None = None) -> None:
    """resolver 테스트용 이력 직접 insert (record_rate_change 부수효과 배제)."""
    async with async_session() as db:
        db.add(
            HourlyRateHistory(
                organization_id=ctx["org_id"],
                org_member_id=ctx["member_id"],
                old_rate=Decimal(old_rate) if old_rate else None,
                new_rate=Decimal(new_rate),
                effective_date=effective,
                reason="__test__",
            )
        )
        await db.commit()


async def _make_schedule_row(ctx: dict, operating_day: date, *, status: str = "confirmed", rate: str = "10.00") -> UUID:
    async with async_session() as db:
        sched = Schedule(
            organization_id=ctx["org_id"],
            user_id=ctx["user_id"],
            store_id=ctx["store_id"],
            operating_day=operating_day,
            start_at=datetime.combine(operating_day, datetime.min.time().replace(hour=9)),
            end_at=datetime.combine(operating_day, datetime.min.time().replace(hour=17)),
            status=status,
            hourly_rate=Decimal(rate),
        )
        db.add(sched)
        await db.commit()
        await db.refresh(sched)
        return sched.id


async def _schedule_rate(schedule_id: UUID) -> Decimal:
    async with async_session() as db:
        return Decimal(
            await db.scalar(
                select(Schedule.hourly_rate).where(Schedule.id == schedule_id)
            )
        )


# ---------------------------------------------------------------------------
# rate_at — 3단 resolver
# ---------------------------------------------------------------------------


async def test_rate_at_member_column_tier(rate_ctx: dict) -> None:
    """②단: 이력 없음 + org_members.hourly_rate 有 → 그 값 (store default 무시)."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    async with async_session() as db:
        rate = await rate_service.rate_at(
            db,
            user_id=rate_ctx["user_id"],
            organization_id=rate_ctx["org_id"],
            store_id=rate_ctx["store_id"],
        )
    assert rate == Decimal("18.50")


async def test_rate_at_store_default_tier(rate_ctx: dict) -> None:
    """③단 store: 개인 rate 없음 → store.default_hourly_rate."""
    async with async_session() as db:
        rate = await rate_service.rate_at(
            db,
            user_id=rate_ctx["user_id"],
            organization_id=rate_ctx["org_id"],
            store_id=rate_ctx["store_id"],
        )
    assert rate == Decimal("17.25")


async def test_rate_at_org_default_tier(rate_ctx: dict) -> None:
    """③단 org: 개인/store 없음 → org.default_hourly_rate (store default NULL 인 매장)."""
    async with async_session() as db:
        bare_store = Store(
            organization_id=rate_ctx["org_id"],
            name=f"__rate_bare_{uuid_mod.uuid4().hex[:8]}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        db.add(bare_store)
        await db.commit()
        await db.refresh(bare_store)
        bare_store_id = bare_store.id
    try:
        async with async_session() as db:
            rate = await rate_service.rate_at(
                db,
                user_id=rate_ctx["user_id"],
                organization_id=rate_ctx["org_id"],
                store_id=bare_store_id,
            )
        assert rate == Decimal("15.00")
    finally:
        async with async_session() as db:
            await db.execute(delete(Store).where(Store.id == bare_store_id))
            await db.commit()


async def test_rate_at_none_when_nothing(rate_ctx: dict) -> None:
    """개인/store/org 어디에도 없으면 None (store/org 미지정 호출)."""
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        rate = await rate_service.rate_at(db, member)
    # member rate NULL + store 미지정 → org default(15.00) 로 떨어진다
    assert rate == Decimal("15.00")
    # organization 을 알 수 없는 경우 (member 없이 user_id 만, org/store 미지정) → None
    async with async_session() as db:
        rate2 = await rate_service.rate_at(db, user_id=rate_ctx["user_id"])
    assert rate2 is None


async def test_rate_at_history_precedence_and_boundaries(rate_ctx: dict) -> None:
    """①단 이력이 ②단 컬럼보다 우선. effective_date ≤ 기준일 경계 포함."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    d1 = date(2026, 8, 1)
    d2 = date(2026, 9, 1)
    await _add_history(rate_ctx, new_rate="20.00", effective=d1, old_rate="18.50")
    await _add_history(rate_ctx, new_rate="22.00", effective=d2, old_rate="20.00")

    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        # 이력 이전 날짜 → member 컬럼
        assert await rate_service.rate_at(db, member, on_date=d1 - timedelta(days=1)) == Decimal("18.50")
        # 경계: effective_date 당일 포함
        assert await rate_service.rate_at(db, member, on_date=d1) == Decimal("20.00")
        # 두 이력 사이 → 최신 due 만
        assert await rate_service.rate_at(db, member, on_date=date(2026, 8, 15)) == Decimal("20.00")
        # 두 번째 경계 당일
        assert await rate_service.rate_at(db, member, on_date=d2) == Decimal("22.00")
        # 이후
        assert await rate_service.rate_at(db, member, on_date=d2 + timedelta(days=30)) == Decimal("22.00")


async def test_person_rate_falls_back_to_users_without_member(
    seed_organization: dict, seed_roles: dict[str, UUID]
) -> None:
    """전환기: org_member 행이 없는 계정은 users.hourly_rate fallback."""
    org_id = seed_organization["id"]
    async with async_session() as db:
        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__rate_nomember_{uuid_mod.uuid4().hex[:8]}",
            full_name="No Member",
            password_hash="x",
            is_active=True,
            hourly_rate=Decimal("21.00"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        user_id = user.id
    try:
        async with async_session() as db:
            rate = await rate_service.person_rate_at(
                db, user_id=user_id, organization_id=org_id
            )
        assert rate == Decimal("21.00")
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


# ---------------------------------------------------------------------------
# record_rate_change — 즉시 적용
# ---------------------------------------------------------------------------


async def test_record_immediate_applies_history_columns_and_schedules(rate_ctx: dict) -> None:
    """즉시 적용: 이력 + org_members + users 미러 + operating_day ≥ 적용일 스케줄 갱신."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    today = _today()
    past_sched = await _make_schedule_row(rate_ctx, today - timedelta(days=3))
    today_sched = await _make_schedule_row(rate_ctx, today)
    future_sched = await _make_schedule_row(rate_ctx, today + timedelta(days=7), status="draft")

    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        row = await rate_service.record_rate_change(
            db, org_member=member, new_rate=19.75, reason="__test__ raise",
            changed_by=None,
        )
        await db.commit()
        assert row is not None
        assert row.new_rate == Decimal("19.75")
        assert row.old_rate == Decimal("18.50")
        assert row.effective_date == today

    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        assert Decimal(member.hourly_rate) == Decimal("19.75")
        users_rate = await db.scalar(
            select(User.hourly_rate).where(User.id == rate_ctx["user_id"])
        )
        assert Decimal(users_rate) == Decimal("19.75")

    # 스케줄: 과거는 그대로, 오늘/미래(draft 포함 전 status)는 갱신
    assert await _schedule_rate(past_sched) == Decimal("10.00")
    assert await _schedule_rate(today_sched) == Decimal("19.75")
    assert await _schedule_rate(future_sched) == Decimal("19.75")


async def test_record_first_change_old_rate_null(rate_ctx: dict) -> None:
    """개인 rate 가 전혀 없던 멤버의 최초 기록 → old_rate NULL."""
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        row = await rate_service.record_rate_change(
            db, org_member=member, new_rate=20, reason="__test__ first",
        )
        await db.commit()
        assert row is not None
        assert row.old_rate is None


async def test_record_future_dated_history_only(rate_ctx: dict) -> None:
    """미래 적용일: 이력만 기록 — 컬럼/미러/스케줄 미변경."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    today = _today()
    future_sched = await _make_schedule_row(rate_ctx, today + timedelta(days=10))

    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        row = await rate_service.record_rate_change(
            db, org_member=member, new_rate=25,
            effective_date=today + timedelta(days=5), reason="__test__ future",
        )
        await db.commit()
        assert row is not None
        assert row.effective_date == today + timedelta(days=5)
        assert row.old_rate == Decimal("18.50")

    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        assert Decimal(member.hourly_rate) == Decimal("18.50")  # 미변경
        users_rate = await db.scalar(
            select(User.hourly_rate).where(User.id == rate_ctx["user_id"])
        )
        assert users_rate is None  # 미러도 미변경 (픽스처 초기값 NULL)
    assert await _schedule_rate(future_sched) == Decimal("10.00")


async def test_record_same_day_reregistration_updates_row(rate_ctx: dict) -> None:
    """같은 날 재등록: insert 대신 기존 행 update, old_rate 는 원본 보존."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        await rate_service.record_rate_change(
            db, org_member=member, new_rate=20, reason="__test__ first",
        )
        await db.commit()
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        await rate_service.record_rate_change(
            db, org_member=member, new_rate=21, reason="__test__ corrected",
        )
        await db.commit()

    async with async_session() as db:
        rows = (
            await db.execute(
                select(HourlyRateHistory).where(
                    HourlyRateHistory.org_member_id == rate_ctx["member_id"]
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].new_rate == Decimal("21.00")
        assert rows[0].old_rate == Decimal("18.50")  # 원본 보존
        assert rows[0].reason == "__test__ corrected"
        member = await db.get(OrgMember, rate_ctx["member_id"])
        assert Decimal(member.hourly_rate) == Decimal("21.00")


async def test_record_old_rate_chains_from_history(rate_ctx: dict) -> None:
    """두 번째 변경(더 뒤 적용일)의 old_rate = 직전 이력의 new_rate."""
    today = _today()
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        await rate_service.record_rate_change(
            db, org_member=member, new_rate=20, reason="__test__ first",
        )
        await db.commit()
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        row = await rate_service.record_rate_change(
            db, org_member=member, new_rate=22,
            effective_date=today + timedelta(days=10), reason="__test__ later",
        )
        await db.commit()
        assert row is not None
        assert row.old_rate == Decimal("20.00")


async def test_record_rejects_non_positive(rate_ctx: dict) -> None:
    """new_rate ≤ 0 → BadRequestError (분기: 0 / 음수)."""
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        with pytest.raises(BadRequestError):
            await rate_service.record_rate_change(db, org_member=member, new_rate=0)
        with pytest.raises(BadRequestError):
            await rate_service.record_rate_change(db, org_member=member, new_rate=-5)


async def test_record_requires_member(rate_ctx: dict) -> None:
    """(user, org) 소속이 없으면 NotFoundError."""
    async with async_session() as db:
        with pytest.raises(NotFoundError):
            await rate_service.record_rate_change(
                db,
                user_id=uuid_mod.uuid4(),  # 존재하지 않는 user
                organization_id=rate_ctx["org_id"],
                new_rate=20,
            )


async def test_record_noop_when_same_value(rate_ctx: dict) -> None:
    """현재 개인 rate 와 동일 값 재등록 → no-op (이력 미생성, None 반환)."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        row = await rate_service.record_rate_change(
            db, org_member=member, new_rate=18.5, reason="__test__ noop",
        )
        await db.commit()
        assert row is None

    async with async_session() as db:
        count = len(
            (
                await db.execute(
                    select(HourlyRateHistory).where(
                        HourlyRateHistory.org_member_id == rate_ctx["member_id"]
                    )
                )
            ).scalars().all()
        )
        assert count == 0


# ---------------------------------------------------------------------------
# apply_due_rate_changes — 도래 반영 + 멱등
# ---------------------------------------------------------------------------


async def test_apply_due_rate_changes_applies_and_is_idempotent(rate_ctx: dict) -> None:
    """적용일 도래(컬럼 미반영) 이력을 반영하고, 재실행은 no-op."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    today = _today()
    # '미래에 기록됐던 변경의 적용일이 오늘 도래' 상황을 직접 insert 로 재현
    await _add_history(rate_ctx, new_rate="23.00", effective=today, old_rate="18.50")
    today_sched = await _make_schedule_row(rate_ctx, today)
    past_sched = await _make_schedule_row(rate_ctx, today - timedelta(days=2))

    async with async_session() as db:
        applied = await rate_service.apply_due_rate_changes(db)
    assert applied >= 1  # 우리 멤버 포함 (다른 잔여 drift 가 있으면 그 이상)

    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        assert Decimal(member.hourly_rate) == Decimal("23.00")
        users_rate = await db.scalar(
            select(User.hourly_rate).where(User.id == rate_ctx["user_id"])
        )
        assert Decimal(users_rate) == Decimal("23.00")
    assert await _schedule_rate(today_sched) == Decimal("23.00")
    assert await _schedule_rate(past_sched) == Decimal("10.00")

    # 멱등: 두 번째 실행은 아무것도 반영하지 않는다
    async with async_session() as db:
        applied_again = await rate_service.apply_due_rate_changes(db)
    assert applied_again == 0


async def test_apply_due_skips_future_effective_dates(rate_ctx: dict) -> None:
    """아직 도래하지 않은 이력은 반영하지 않는다."""
    await _set_member_rate(rate_ctx["member_id"], Decimal("18.50"))
    await _add_history(
        rate_ctx, new_rate="30.00", effective=_today() + timedelta(days=3),
        old_rate="18.50",
    )
    async with async_session() as db:
        applied = await rate_service.apply_due_rate_changes(db)
    assert applied == 0
    async with async_session() as db:
        member = await db.get(OrgMember, rate_ctx["member_id"])
        assert Decimal(member.hourly_rate) == Decimal("18.50")
