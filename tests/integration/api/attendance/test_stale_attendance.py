"""Integration tests — Issue 11 (이전 work_date 미완료 orphan attendance 경고).

identify-by-pin 이 이전 work_date 의 미완료(clock_in 있고 clock_out 없는 working/
on_break/late) attendance 를 stale_attendances 로 반환하는지.
  - 최근 30일만 (초과 제외)
  - 현재 기기 매장만
  - clocked_out / 완료된 건은 제외
  - 최신순 정렬
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session


pytestmark = pytest.mark.asyncio


async def _insert_attendance(
    *,
    user_id: UUID,
    store_id: UUID,
    org_id: UUID,
    work_date: date,
    clock_in: datetime | None,
    clock_out: datetime | None,
    status: str,
) -> None:
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO attendances "
                "(id, organization_id, store_id, user_id, work_date, clock_in, clock_out, "
                " clock_in_timezone, status, created_at, updated_at) "
                "VALUES (:id, :org, :sid, :uid, :wd, :ci, :co, 'America/Los_Angeles', :st, now(), now())"
            ),
            {
                "id": uuid4(), "org": org_id, "sid": store_id, "uid": user_id,
                "wd": work_date, "ci": clock_in, "co": clock_out, "st": status,
            },
        )
        await db.commit()


async def _org_id(store_id: UUID) -> UUID:
    async with async_session() as db:
        return (await db.execute(
            text("SELECT organization_id FROM stores WHERE id = :sid"),
            {"sid": store_id},
        )).scalar_one()


async def _store_today(store_id: UUID) -> date:
    """server 와 동일한 store-tz work_date (date.today() 와 다를 수 있음)."""
    from app.utils.timezone import get_store_day_config, get_work_date
    async with async_session() as db:
        tz_name, day_cfg = await get_store_day_config(db, store_id)
    return get_work_date(tz_name, day_cfg, datetime.now(timezone.utc))


async def test_stale_attendance_returned_for_orphan_records(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """이전 work_date 의 미완료(clock_in O, clock_out X, working) → stale_attendances 포함."""
    org = await _org_id(test_store_id)
    today = await _store_today(test_store_id)
    # 3일 전 미완료 (working)
    await _insert_attendance(
        user_id=test_user["id"], store_id=test_store_id, org_id=org,
        work_date=today - timedelta(days=3),
        clock_in=datetime.now(timezone.utc) - timedelta(days=3),
        clock_out=None, status="working",
    )

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    stale = resp.json()["stale_attendances"]
    assert len(stale) >= 1
    assert any(s["status"] == "working" for s in stale)


async def test_stale_excludes_clocked_out_and_beyond_30days(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """clocked_out(완료) 와 30일 초과는 stale 에서 제외."""
    org = await _org_id(test_store_id)
    today = await _store_today(test_store_id)
    # 완료된 어제 (clocked_out) — 제외돼야
    await _insert_attendance(
        user_id=test_user["id"], store_id=test_store_id, org_id=org,
        work_date=today - timedelta(days=1),
        clock_in=datetime.now(timezone.utc) - timedelta(days=1, hours=8),
        clock_out=datetime.now(timezone.utc) - timedelta(days=1),
        status="clocked_out",
    )
    # 40일 전 미완료 — 30일 초과로 제외돼야
    await _insert_attendance(
        user_id=test_user["id"], store_id=test_store_id, org_id=org,
        work_date=today - timedelta(days=40),
        clock_in=datetime.now(timezone.utc) - timedelta(days=40),
        clock_out=None, status="working",
    )

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    stale = resp.json()["stale_attendances"]
    # clocked_out / 40일 전은 없어야
    assert all(s["status"] != "clocked_out" for s in stale)
    wds = {s["work_date"] for s in stale}
    assert str(today - timedelta(days=40)) not in wds
    assert str(today - timedelta(days=1)) not in wds


async def test_stale_sorted_desc_by_work_date(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
) -> None:
    """여러 미완료 → 최신순 (work_date desc) 정렬."""
    org = await _org_id(test_store_id)
    today = await _store_today(test_store_id)
    for d in (2, 5, 10):
        await _insert_attendance(
            user_id=test_user["id"], store_id=test_store_id, org_id=org,
            work_date=today - timedelta(days=d),
            clock_in=datetime.now(timezone.utc) - timedelta(days=d),
            clock_out=None, status="working",
        )

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    stale = resp.json()["stale_attendances"]
    wds = [s["work_date"] for s in stale]
    # 최신(2일 전)이 먼저
    assert wds == sorted(wds, reverse=True)
    assert wds[0] == str(today - timedelta(days=2))


async def test_no_stale_when_none(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
) -> None:
    """미완료 기록 없으면 stale_attendances 빈 list."""
    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stale_attendances"] == []
