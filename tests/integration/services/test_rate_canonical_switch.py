"""Integration — R6 canonical switch: 개인 시급 읽기가 org_members 를 본다.

대상 (users.hourly_rate → org_members 전환, Payroll v1 Phase 1):
    - schedule_service._resolve_hourly_rate_with_source
    - schedule_service._list_to_responses (배치 경로 person_rates_map)
    - schedule_service.create_walk_in_schedule
    - schedule_request_service._resolve_hourly_rate
    - user_service.get_user (raw hourly_rate 소스 + effective store-tier 버그 수정)

증명 방식: users.hourly_rate 를 org_members 와 **다른 값**으로 심어 두고
org_members 값이 나오는지 확인 — users 를 읽으면 즉시 실패한다.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime, timezone
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
from app.models.user_store import UserStore
from app.services.schedule_request_service import schedule_request_service
from app.services.schedule_service import schedule_service
from app.services.user_service import user_service

pytestmark = pytest.mark.asyncio

# users.hourly_rate 에 심는 '오답' 값 — 이 값이 나오면 canonical switch 실패
DECOY_USERS_RATE = Decimal("99.00")
MEMBER_RATE = Decimal("30.00")
STORE_RATE = Decimal("17.25")


@pytest_asyncio.fixture
async def switch_ctx(seed_organization: dict, seed_roles: dict[str, UUID]) -> AsyncIterator[dict]:
    """user(오답 99) + org_member(정답 30) + store(default 17.25) 컨텍스트."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        org = await db.get(Organization, org_id)
        original_org_rate = org.default_hourly_rate
        org.default_hourly_rate = Decimal("15.00")

        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__rate_switch_{suffix}",
            full_name="Rate Switch Test",
            password_hash="x",
            is_active=True,
            hourly_rate=DECOY_USERS_RATE,  # 오답 — 읽히면 안 됨
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        member = OrgMember(
            user_id=user.id,
            organization_id=org_id,
            role_id=seed_roles["staff"],
            hourly_rate=MEMBER_RATE,  # 정답 (canonical)
        )
        store = Store(
            organization_id=org_id,
            name=f"__rate_switch_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
            default_hourly_rate=STORE_RATE,
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
        await db.execute(delete(UserStore).where(UserStore.user_id == ctx["user_id"]))
        await db.execute(delete(OrgMember).where(OrgMember.id == ctx["member_id"]))
        await db.execute(delete(User).where(User.id == ctx["user_id"]))
        await db.execute(delete(Store).where(Store.id == ctx["store_id"]))
        org = await db.get(Organization, org_id)
        org.default_hourly_rate = original_org_rate
        await db.commit()


async def _clear_member_rate(member_id: UUID) -> None:
    async with async_session() as db:
        member = await db.get(OrgMember, member_id)
        member.hourly_rate = None
        await db.commit()


# ---------------------------------------------------------------------------
# schedule_service 단건 resolver
# ---------------------------------------------------------------------------


async def test_schedule_resolver_uses_org_members(switch_ctx: dict) -> None:
    """_resolve_hourly_rate_with_source → org_members 값 (users 99 무시)."""
    async with async_session() as db:
        rate, source = await schedule_service._resolve_hourly_rate_with_source(
            db, switch_ctx["user_id"], switch_ctx["store_id"], switch_ctx["org_id"],
        )
    assert (rate, source) == (float(MEMBER_RATE), "user")


async def test_schedule_resolver_store_tier_when_member_rate_null(switch_ctx: dict) -> None:
    """member 행이 있고 rate NULL → users(99) 로 fallback 하지 않고 store 로."""
    await _clear_member_rate(switch_ctx["member_id"])
    async with async_session() as db:
        rate, source = await schedule_service._resolve_hourly_rate_with_source(
            db, switch_ctx["user_id"], switch_ctx["store_id"], switch_ctx["org_id"],
        )
    assert (rate, source) == (float(STORE_RATE), "store")


async def test_schedule_resolver_history_precedence(switch_ctx: dict) -> None:
    """이력이 있으면 member 컬럼보다 이력이 우선 (rate_at ①단 경유)."""
    async with async_session() as db:
        db.add(
            HourlyRateHistory(
                organization_id=switch_ctx["org_id"],
                org_member_id=switch_ctx["member_id"],
                old_rate=MEMBER_RATE,
                new_rate=Decimal("33.00"),
                effective_date=datetime.now(timezone.utc).date(),
                reason="__test__",
            )
        )
        await db.commit()
    async with async_session() as db:
        rate, source = await schedule_service._resolve_hourly_rate_with_source(
            db, switch_ctx["user_id"], switch_ctx["store_id"], switch_ctx["org_id"],
        )
    assert (rate, source) == (33.0, "user")


# ---------------------------------------------------------------------------
# schedule_service 배치 경로 (_list_to_responses)
# ---------------------------------------------------------------------------


async def test_batch_list_responses_use_org_members(switch_ctx: dict) -> None:
    """배치 목록 변환의 effective_rate 도 org_members 소스."""
    today = datetime.now(timezone.utc).date()
    async with async_session() as db:
        sched = Schedule(
            organization_id=switch_ctx["org_id"],
            user_id=switch_ctx["user_id"],
            store_id=switch_ctx["store_id"],
            operating_day=today,
            start_at=datetime.combine(today, datetime.min.time().replace(hour=9)),
            end_at=datetime.combine(today, datetime.min.time().replace(hour=17)),
            status="confirmed",
            hourly_rate=Decimal("0"),  # 저장 rate 0 → cascade resolve 경로
        )
        db.add(sched)
        await db.commit()
        await db.refresh(sched)

        responses = await schedule_service._list_to_responses(db, [sched])
    assert len(responses) == 1
    assert responses[0].effective_rate == float(MEMBER_RATE)
    assert responses[0].effective_rate_source == "user"


# ---------------------------------------------------------------------------
# 워크인 스케줄 생성
# ---------------------------------------------------------------------------


async def test_walk_in_uses_org_members(switch_ctx: dict) -> None:
    """create_walk_in_schedule 의 hourly_rate = org_members 값 (users 99 무시)."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        entry = await schedule_service.create_walk_in_schedule(
            db,
            organization_id=switch_ctx["org_id"],
            store_id=switch_ctx["store_id"],
            user_id=switch_ctx["user_id"],
            work_date=now.date(),
            clock_in_at=now,
            store_tz="UTC",
            created_by=None,
        )
        assert float(entry.hourly_rate) == float(MEMBER_RATE)
        # commit 하지 않는 서비스 — rollback 으로 부수효과(attendance 등) 폐기
        await db.rollback()


async def test_walk_in_zero_when_no_person_rate(switch_ctx: dict) -> None:
    """개인 rate 없으면 0 (D3) — store default 로 오염되지 않는다."""
    await _clear_member_rate(switch_ctx["member_id"])
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        entry = await schedule_service.create_walk_in_schedule(
            db,
            organization_id=switch_ctx["org_id"],
            store_id=switch_ctx["store_id"],
            user_id=switch_ctx["user_id"],
            work_date=now.date(),
            clock_in_at=now,
            store_tz="UTC",
            created_by=None,
        )
        assert float(entry.hourly_rate) == 0.0
        await db.rollback()


# ---------------------------------------------------------------------------
# schedule_request_service resolver
# ---------------------------------------------------------------------------


async def test_request_resolver_uses_org_members(switch_ctx: dict) -> None:
    """schedule_request 의 cascade 도 org_members 소스."""
    async with async_session() as db:
        rate = await schedule_request_service._resolve_hourly_rate(
            db, switch_ctx["user_id"], switch_ctx["store_id"],
        )
    assert rate == float(MEMBER_RATE)


async def test_request_resolver_store_fallback(switch_ctx: dict) -> None:
    """member rate NULL → store default (users 99 무시)."""
    await _clear_member_rate(switch_ctx["member_id"])
    async with async_session() as db:
        rate = await schedule_request_service._resolve_hourly_rate(
            db, switch_ctx["user_id"], switch_ctx["store_id"],
        )
    assert rate == float(STORE_RATE)


async def test_request_resolver_override_wins(switch_ctx: dict) -> None:
    """override 는 여전히 최우선."""
    async with async_session() as db:
        rate = await schedule_request_service._resolve_hourly_rate(
            db, switch_ctx["user_id"], switch_ctx["store_id"], override=50.0,
        )
    assert rate == 50.0


# ---------------------------------------------------------------------------
# user_service — raw rate 소스 + effective store-tier 버그 수정
# ---------------------------------------------------------------------------


async def test_user_response_rate_sourced_from_org_members(switch_ctx: dict) -> None:
    """GET user 응답의 hourly_rate = org_members 값 (users 99 무시)."""
    async with async_session() as db:
        resp = await user_service.get_user(
            db, switch_ctx["user_id"], switch_ctx["org_id"]
        )
    assert resp.hourly_rate == float(MEMBER_RATE)
    assert resp.effective_hourly_rate == float(MEMBER_RATE)


async def test_effective_rate_store_tier_fix(switch_ctx: dict) -> None:
    """개인 rate 없음 + 배정 매장 default 有 → effective = store default.

    (기존 버그: store 단계를 건너뛰고 org default 로 떨어졌다 — user>store>org 정렬)
    """
    await _clear_member_rate(switch_ctx["member_id"])
    async with async_session() as db:
        # users 쪽 개인 rate 도 비워 상속 상태로
        user = await db.get(User, switch_ctx["user_id"])
        user.hourly_rate = None
        db.add(
            UserStore(user_id=switch_ctx["user_id"], store_id=switch_ctx["store_id"])
        )
        await db.commit()

    async with async_session() as db:
        resp = await user_service.get_user(
            db, switch_ctx["user_id"], switch_ctx["org_id"]
        )
    assert resp.hourly_rate is None
    assert resp.effective_hourly_rate == float(STORE_RATE)  # org(15) 아님 — store 우선


async def test_effective_rate_org_fallback_without_store(switch_ctx: dict) -> None:
    """개인/store 없음 → org default (기존 동작 보존)."""
    await _clear_member_rate(switch_ctx["member_id"])
    async with async_session() as db:
        user = await db.get(User, switch_ctx["user_id"])
        user.hourly_rate = None
        await db.commit()

    async with async_session() as db:
        resp = await user_service.get_user(
            db, switch_ctx["user_id"], switch_ctx["org_id"]
        )
    assert resp.hourly_rate is None
    assert resp.effective_hourly_rate == 15.0
