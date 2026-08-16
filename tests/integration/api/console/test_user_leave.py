"""API integration — 휴직 (POST /console/users/{id}/leave · /leave/return).

검증 (조직 계층 트랙 §6 · D5):
- 휴직 시작: status='on_leave' + 기간/분류/유급 기록, 계정은 살아 있음
- 휴직자는 스케줄 배정 후보에서 제외되지만, 이미 근무한 기록이 있으면 로스터에는 남는다
- schedule_action 필수 (keep 은 정당한 선택지 — 퇴사와 다르다)
- 복귀: status='active', 휴직 이력은 보존
- 복귀 예정일 경과 시 자동 복귀 (apply_due_returns)
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.schedule import Schedule
from app.models.user import User
from app.models.user_store import UserStore
from app.services.leave_service import leave_service

pytestmark = pytest.mark.asyncio

LEAVE_START = date(2030, 9, 2)
LEAVE_END = LEAVE_START + timedelta(days=14)
DURING = LEAVE_START + timedelta(days=3)
BEFORE = LEAVE_START - timedelta(days=3)
ROSTER_URL = "/api/v1/console/schedules/roster"


def _leave_url(uid: str) -> str:
    return f"/api/v1/console/users/{uid}/leave"


@pytest_asyncio.fixture
async def leave_target(test_user, test_store_id) -> AsyncIterator[dict]:
    """teststaff 를 매장에 배정하고 휴직 전/중 스케줄을 하나씩 심는다. 테스트 후 복원."""
    uid = test_user["id"]
    org_id = test_user["organization_id"]
    async with async_session() as db:
        existing = await db.scalar(select(UserStore).where(
            UserStore.user_id == uid, UserStore.store_id == test_store_id,
        ))
        created = existing is None
        if created:
            db.add(UserStore(
                user_id=uid, store_id=test_store_id,
                is_manager=False, is_work_assignment=True,
            ))
        for day in (BEFORE, DURING):
            db.add(Schedule(
                organization_id=org_id, user_id=uid, store_id=test_store_id,
                operating_day=day, status="confirmed", net_work_minutes=480,
                start_at=datetime.combine(day, time(9, 0)),
                end_at=datetime.combine(day, time(17, 0)),
            ))
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid, OrgMember.organization_id == org_id,
        ))
        if member:
            member.status = "active"
        await db.commit()
    try:
        yield test_user
    finally:
        async with async_session() as db:
            await db.execute(delete(Schedule).where(
                Schedule.store_id == test_store_id,
                Schedule.operating_day.in_([BEFORE, DURING]),
            ))
            member = await db.scalar(select(OrgMember).where(
                OrgMember.user_id == uid, OrgMember.organization_id == org_id,
            ))
            if member:
                member.status = "active"
                member.leave_start_date = None
                member.leave_end_date = None
                member.leave_type = None
                member.leave_is_paid = None
                member.leave_note = None
            user = await db.get(User, uid)
            user.is_active = True
            if created:
                await db.execute(delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == test_store_id,
                ))
            await db.commit()


async def _member(uid, org_id) -> OrgMember:
    async with async_session() as db:
        return await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid, OrgMember.organization_id == org_id,
        ))


async def test_start_leave_records_and_keeps_account(
    async_client, admin_headers, leave_target
):
    """휴직 시작 — 상태·기간·분류 기록, 계정은 살아 있다(앱 접근 유지)."""
    uid = str(leave_target["id"])
    resp = await async_client.post(_leave_url(uid), headers=admin_headers, json={
        "start_date": LEAVE_START.isoformat(),
        "end_date": LEAVE_END.isoformat(),
        "schedule_action": "unassign",
        "leave_type": "Medical",
        "is_paid": False,
        "note": "Surgery recovery",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "on_leave"
    assert body["schedules_affected"] == 1  # 휴직 기간의 1건만

    member = await _member(leave_target["id"], leave_target["organization_id"])
    assert member.status == "on_leave"
    assert member.leave_start_date == LEAVE_START
    assert member.leave_end_date == LEAVE_END
    assert member.leave_type == "Medical"
    assert member.leave_is_paid is False

    async with async_session() as db:
        user = await db.get(User, leave_target["id"])
    # 퇴사와 달리 계정을 죽이지 않는다
    assert user.is_active is True


async def test_start_leave_keeps_schedules_when_requested(
    async_client, admin_headers, leave_target, test_store_id
):
    """keep — 짧은 휴직이면 스케줄을 그대로 둘 수 있다 (퇴사와 다른 점)."""
    uid = str(leave_target["id"])
    resp = await async_client.post(_leave_url(uid), headers=admin_headers, json={
        "start_date": LEAVE_START.isoformat(),
        "schedule_action": "keep",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["schedules_affected"] == 0

    async with async_session() as db:
        rows = (await db.execute(select(Schedule).where(
            Schedule.store_id == test_store_id,
            Schedule.operating_day == DURING,
        ))).scalars().all()
    assert len(rows) == 1
    assert str(rows[0].user_id) == uid


async def test_start_leave_requires_schedule_action(
    async_client, admin_headers, leave_target
):
    resp = await async_client.post(
        _leave_url(str(leave_target["id"])), headers=admin_headers,
        json={"start_date": LEAVE_START.isoformat()},
    )
    assert resp.status_code == 422, resp.text


async def test_start_leave_rejects_end_before_start(
    async_client, admin_headers, leave_target
):
    resp = await async_client.post(
        _leave_url(str(leave_target["id"])), headers=admin_headers,
        json={
            "start_date": LEAVE_START.isoformat(),
            "end_date": (LEAVE_START - timedelta(days=1)).isoformat(),
            "schedule_action": "keep",
        },
    )
    assert resp.status_code == 400, resp.text


async def test_on_leave_excluded_from_roster_but_past_record_kept(
    async_client, admin_headers, leave_target, test_store_id
):
    """휴직자는 새 배정 후보가 아니지만, 이미 근무한 기간에는 행이 남는다 (fail-open)."""
    uid = str(leave_target["id"])
    await async_client.post(_leave_url(uid), headers=admin_headers, json={
        "start_date": LEAVE_START.isoformat(),
        "schedule_action": "keep",
    })

    # 근무 기록이 있는 날 → 보인다
    resp = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": BEFORE.isoformat(), "date_to": BEFORE.isoformat(),
        "granularity": "week", "store_ids": str(test_store_id),
    })
    assert resp.status_code == 200, resp.text
    assert uid in {r["user_id"] for r in resp.json()["roster"]}

    # 기록이 없는 날 → 후보에서 빠진다
    empty_day = (LEAVE_END + timedelta(days=30)).isoformat()
    resp2 = await async_client.get(ROSTER_URL, headers=admin_headers, params={
        "date_from": empty_day, "date_to": empty_day,
        "granularity": "week", "store_ids": str(test_store_id),
    })
    assert resp2.status_code == 200, resp2.text
    assert uid not in {r["user_id"] for r in resp2.json()["roster"]}


async def test_end_leave_returns_to_active_and_keeps_history(
    async_client, admin_headers, leave_target
):
    uid = str(leave_target["id"])
    await async_client.post(_leave_url(uid), headers=admin_headers, json={
        "start_date": LEAVE_START.isoformat(),
        "leave_type": "Family",
        "schedule_action": "keep",
    })
    resp = await async_client.post(f"{_leave_url(uid)}/return", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    member = await _member(leave_target["id"], leave_target["organization_id"])
    assert member.status == "active"
    # 이력은 지우지 않는다
    assert member.leave_start_date == LEAVE_START
    assert member.leave_type == "Family"


async def test_end_leave_rejects_when_not_on_leave(
    async_client, admin_headers, leave_target
):
    resp = await async_client.post(
        f"{_leave_url(str(leave_target['id']))}/return", headers=admin_headers
    )
    assert resp.status_code == 400, resp.text


async def test_apply_due_returns_auto_restores(
    async_client, admin_headers, leave_target
):
    """복귀 예정일이 지나면 자동 복귀. 멱등 — 두 번째 호출은 0건."""
    uid = str(leave_target["id"])
    await async_client.post(_leave_url(uid), headers=admin_headers, json={
        "start_date": LEAVE_START.isoformat(),
        "end_date": LEAVE_END.isoformat(),
        "schedule_action": "keep",
    })
    org_id = leave_target["organization_id"]
    async with async_session() as db:
        count = await leave_service.apply_due_returns(
            db, org_id, today=LEAVE_END + timedelta(days=1)
        )
    assert count == 1
    member = await _member(leave_target["id"], org_id)
    assert member.status == "active"

    async with async_session() as db:
        again = await leave_service.apply_due_returns(
            db, org_id, today=LEAVE_END + timedelta(days=1)
        )
    assert again == 0
