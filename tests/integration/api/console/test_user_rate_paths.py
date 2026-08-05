"""API integration — hourly_rate 쓰기 경로 통일 (Payroll v1 Phase 1).

대상:
    - PUT   /api/v1/console/users/{user_id}   (hourly_rate 변경 → record_rate_change)
    - PATCH /api/v1/console/users/bulk        (hourly_rate 일괄 → record_rate_change)

검증:
    - 이력(hourly_rate_history) 생성 + org_members(canonical)/users(미러) dual-write
    - 같은 날 재등록은 기존 이력 행 update (old_rate 원본 보존)
    - null 명시 = 해제 — 이력 없이 양쪽 컬럼 NULL
    - 0 이하 값 → 400
    - rate 변경 시 operating_day ≥ 오늘 스케줄 표시 rate 갱신 (과거는 보존)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.rate import HourlyRateHistory
from app.models.schedule import Schedule
from app.models.user import User

pytestmark = pytest.mark.asyncio


def _today():
    return datetime.now(timezone.utc).date()


async def _member_id(user_id: UUID, org_id: UUID) -> UUID:
    async with async_session() as db:
        return (
            await db.execute(
                select(OrgMember.id).where(
                    OrgMember.user_id == user_id,
                    OrgMember.organization_id == org_id,
                )
            )
        ).scalar_one()


async def _history_rows(member_id: UUID) -> list[HourlyRateHistory]:
    async with async_session() as db:
        return list(
            (
                await db.execute(
                    select(HourlyRateHistory).where(
                        HourlyRateHistory.org_member_id == member_id
                    )
                )
            ).scalars().all()
        )


async def _column_rates(user_id: UUID, org_id: UUID) -> tuple[Decimal | None, Decimal | None]:
    """(org_members.hourly_rate, users.hourly_rate)."""
    async with async_session() as db:
        member_rate = await db.scalar(
            select(OrgMember.hourly_rate).where(
                OrgMember.user_id == user_id,
                OrgMember.organization_id == org_id,
            )
        )
        users_rate = await db.scalar(
            select(User.hourly_rate).where(User.id == user_id)
        )
        return (
            Decimal(member_rate) if member_rate is not None else None,
            Decimal(users_rate) if users_rate is not None else None,
        )


@pytest_asyncio.fixture
async def _restore_rates(test_users: dict):
    """teststaff/testsv 의 이력/컬럼 rate 를 테스트 전후 초기화."""
    uids = [test_users["teststaff"]["id"], test_users["testsv"]["id"]]

    async def _cleanup() -> None:
        async with async_session() as db:
            member_ids = (
                await db.execute(
                    select(OrgMember.id).where(OrgMember.user_id.in_(uids))
                )
            ).scalars().all()
            await db.execute(
                delete(HourlyRateHistory).where(
                    HourlyRateHistory.org_member_id.in_(member_ids)
                )
            )
            await db.execute(
                update(OrgMember)
                .where(OrgMember.id.in_(member_ids))
                .values(hourly_rate=None)
            )
            await db.execute(
                update(User).where(User.id.in_(uids)).values(hourly_rate=None)
            )
            await db.commit()

    await _cleanup()
    yield
    await _cleanup()


# ---------------------------------------------------------------------------
# PUT /users/{id} — 단건 시급 변경
# ---------------------------------------------------------------------------


async def test_put_user_rate_creates_history_and_dual_writes(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_users: dict,
    _restore_rates,
) -> None:
    """PUT hourly_rate → 이력 1건(old NULL) + org_members/users 동시 기록 + 응답 반영."""
    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}",
        headers=admin_headers,
        json={"hourly_rate": 21.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hourly_rate"] == 21.5

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    rows = await _history_rows(member_id)
    assert len(rows) == 1
    assert rows[0].new_rate == Decimal("21.50")
    assert rows[0].old_rate is None  # 최초 기록
    assert rows[0].effective_date == _today()
    assert rows[0].changed_by == test_users["testadmin"]["id"]
    assert rows[0].reason == "Updated via console (user update)"

    member_rate, users_rate = await _column_rates(
        test_user["id"], test_user["organization_id"]
    )
    assert member_rate == Decimal("21.50")  # canonical
    assert users_rate == Decimal("21.50")  # 전환기 미러


async def test_put_user_rate_same_day_updates_existing_row(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """같은 날 두 번 변경 → 이력은 1행 유지(update), old_rate 원본 보존."""
    for value in (21.5, 23.0):
        resp = await async_client.put(
            f"/api/v1/console/users/{test_user['id']}",
            headers=admin_headers,
            json={"hourly_rate": value},
        )
        assert resp.status_code == 200, resp.text

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    rows = await _history_rows(member_id)
    assert len(rows) == 1
    assert rows[0].new_rate == Decimal("23.00")
    assert rows[0].old_rate is None  # 첫 기록의 old(NULL) 보존

    member_rate, users_rate = await _column_rates(
        test_user["id"], test_user["organization_id"]
    )
    assert member_rate == Decimal("23.00")
    assert users_rate == Decimal("23.00")


async def test_put_user_rate_null_clears_without_history(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """null 명시 = 해제 — 양쪽 컬럼 NULL, 해제 자체는 이력화하지 않는다."""
    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}",
        headers=admin_headers,
        json={"hourly_rate": 21.5},
    )
    assert resp.status_code == 200, resp.text

    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}",
        headers=admin_headers,
        json={"hourly_rate": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hourly_rate"] is None

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    rows = await _history_rows(member_id)
    assert len(rows) == 1  # 해제로 늘어나지 않음 (CHECK new_rate > 0)

    member_rate, users_rate = await _column_rates(
        test_user["id"], test_user["organization_id"]
    )
    assert member_rate is None
    assert users_rate is None


async def test_put_user_rate_zero_rejected(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """0 시급 → 400 (record_rate_change 검증)."""
    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}",
        headers=admin_headers,
        json={"hourly_rate": 0},
    )
    assert resp.status_code == 400, resp.text

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_rows(member_id) == []


async def test_put_user_rate_refreshes_schedules_from_today(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    make_schedule,
    _restore_rates,
) -> None:
    """rate 변경 시 operating_day ≥ 오늘 스케줄만 표시 rate 갱신 (과거 보존)."""
    today = _today()
    past_id = await make_schedule(test_user, work_date=today - timedelta(days=3))
    today_id = await make_schedule(test_user, work_date=today)
    future_id = await make_schedule(test_user, work_date=today + timedelta(days=5))

    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}",
        headers=admin_headers,
        json={"hourly_rate": 21.5},
    )
    assert resp.status_code == 200, resp.text

    async with async_session() as db:
        rates = {
            sid: Decimal(
                await db.scalar(select(Schedule.hourly_rate).where(Schedule.id == sid))
            )
            for sid in (past_id, today_id, future_id)
        }
    assert rates[past_id] == Decimal("0.00")  # 과거는 그대로 (생성 default 0)
    assert rates[today_id] == Decimal("21.50")
    assert rates[future_id] == Decimal("21.50")


# ---------------------------------------------------------------------------
# PATCH /users/bulk — 일괄 시급 변경
# ---------------------------------------------------------------------------


async def test_bulk_update_rate_creates_history_per_user(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    _restore_rates,
) -> None:
    """bulk hourly_rate → 각 유저 이력 + dual-write, updated_count 일치."""
    staff = test_users["teststaff"]
    sv = test_users["testsv"]
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={
            "user_ids": [str(staff["id"]), str(sv["id"])],
            "hourly_rate": 19.25,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 2

    for info in (staff, sv):
        member_id = await _member_id(info["id"], info["organization_id"])
        rows = await _history_rows(member_id)
        assert len(rows) == 1
        assert rows[0].new_rate == Decimal("19.25")
        assert rows[0].reason == "Updated via console (bulk update)"
        member_rate, users_rate = await _column_rates(
            info["id"], info["organization_id"]
        )
        assert member_rate == Decimal("19.25")
        assert users_rate == Decimal("19.25")


async def test_bulk_update_rate_null_clears_without_history(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    _restore_rates,
) -> None:
    """bulk null = 일괄 해제 — 이력 없이 양쪽 컬럼 NULL."""
    staff = test_users["teststaff"]
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [str(staff["id"])], "hourly_rate": 19.25},
    )
    assert resp.status_code == 200, resp.text

    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [str(staff["id"])], "hourly_rate": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1

    member_id = await _member_id(staff["id"], staff["organization_id"])
    assert len(await _history_rows(member_id)) == 1  # 해제로 안 늘어남
    member_rate, users_rate = await _column_rates(
        staff["id"], staff["organization_id"]
    )
    assert member_rate is None
    assert users_rate is None


async def test_bulk_update_mixed_fields_still_applies_direct_columns(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    _restore_rates,
) -> None:
    """hourly_rate + department 동시 bulk — rate 는 이력 경로, 나머진 직접 컬럼."""
    staff = test_users["teststaff"]
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={
            "user_ids": [str(staff["id"])],
            "hourly_rate": 20.0,
            "department": "FOH",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1

    member_id = await _member_id(staff["id"], staff["organization_id"])
    assert len(await _history_rows(member_id)) == 1
    async with async_session() as db:
        dept = await db.scalar(select(User.department).where(User.id == staff["id"]))
    assert dept == "FOH"

    # 원복 (department 는 restore fixture 대상 아님)
    async with async_session() as db:
        await db.execute(
            update(User).where(User.id == staff["id"]).values(department=None)
        )
        await db.commit()
