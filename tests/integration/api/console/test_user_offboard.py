"""API integration — Offboard (POST /api/v1/console/users/{id}/offboard).

검증 (조직 계층 트랙 §6 · D3):
- 재직 상태/퇴사일/사유/재고용 여부 기록 + 계정 비활성 + PIN 회수
- 퇴사일 **이후** 스케줄만 처리 (unassign / delete), 지난 근무는 그대로
- future_schedule_action 은 필수 (기본값 없음)
- 미가입(유령) 계정은 퇴사 처리 대상이 아님
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

pytestmark = pytest.mark.asyncio

OFFBOARD_DAY = date(2030, 6, 15)  # 다른 테스트와 겹치지 않는 고정일
PAST_DAY = OFFBOARD_DAY - timedelta(days=7)
FUTURE_DAY = OFFBOARD_DAY + timedelta(days=7)


def _url(user_id: str) -> str:
    return f"/api/v1/console/users/{user_id}/offboard"


@pytest_asyncio.fixture
async def offboard_target(test_user, test_store_id) -> AsyncIterator[dict]:
    """teststaff 를 매장에 배정하고 과거·미래 스케줄을 하나씩 심는다. 테스트 후 전부 복원."""
    uid = test_user["id"]
    org_id = test_user["organization_id"]

    async with async_session() as db:
        existing = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == uid, UserStore.store_id == test_store_id
            )
        )
        created_store_link = existing is None
        if created_store_link:
            db.add(UserStore(
                user_id=uid, store_id=test_store_id,
                is_manager=False, is_work_assignment=True,
            ))
        for day in (PAST_DAY, FUTURE_DAY):
            db.add(Schedule(
                organization_id=org_id, user_id=uid, store_id=test_store_id,
                operating_day=day, status="confirmed", net_work_minutes=480,
                start_at=datetime.combine(day, time(9, 0)),
                end_at=datetime.combine(day, time(17, 0)),
            ))
        # 퇴사 전 상태 보장 (다른 테스트 잔재 방지)
        member = await db.scalar(
            select(OrgMember).where(
                OrgMember.user_id == uid, OrgMember.organization_id == org_id
            )
        )
        prev_status = member.status if member else None
        if member:
            member.status = "active"
            member.termination_date = None
        user = await db.get(User, uid)
        user.is_active = True
        await db.commit()

    try:
        yield test_user
    finally:
        async with async_session() as db:
            await db.execute(delete(Schedule).where(
                Schedule.user_id == uid,
                Schedule.operating_day.in_([PAST_DAY, FUTURE_DAY]),
            ))
            # unassign 된 행은 user_id 가 NULL 이라 위 조건에 안 걸린다 — 날짜로 한 번 더
            await db.execute(delete(Schedule).where(
                Schedule.store_id == test_store_id,
                Schedule.operating_day.in_([PAST_DAY, FUTURE_DAY]),
            ))
            member = await db.scalar(
                select(OrgMember).where(
                    OrgMember.user_id == uid, OrgMember.organization_id == org_id
                )
            )
            if member:
                member.status = prev_status or "active"
                member.termination_date = None
                member.termination_reason = None
                member.rehire_eligible = None
            user = await db.get(User, uid)
            user.is_active = True
            if created_store_link:
                await db.execute(delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == test_store_id,
                ))
            await db.commit()


async def _schedules(uid: str, store_id) -> dict[date, tuple[str | None, str]]:
    """{영업일: (user_id, status)} — 퇴사 처리 후 상태 확인용."""
    async with async_session() as db:
        rows = (await db.execute(
            select(Schedule).where(
                Schedule.store_id == store_id,
                Schedule.operating_day.in_([PAST_DAY, FUTURE_DAY]),
            )
        )).scalars().all()
    return {
        r.operating_day: (str(r.user_id) if r.user_id else None, r.status)
        for r in rows
    }


async def test_offboard_unassigns_future_keeps_past(
    async_client, admin_headers, offboard_target, test_store_id
):
    """unassign: 미래 스케줄은 사람만 비우고, 과거 근무는 손대지 않는다."""
    uid = str(offboard_target["id"])
    resp = await async_client.post(_url(uid), headers=admin_headers, json={
        "termination_date": OFFBOARD_DAY.isoformat(),
        "future_schedule_action": "unassign",
        "reason": "Moved out of state",
        "rehire_eligible": True,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["future_schedules_affected"] == 1
    assert body["future_schedule_action"] == "unassign"

    sched = await _schedules(uid, test_store_id)
    # 과거: 그대로
    assert sched[PAST_DAY] == (uid, "confirmed")
    # 미래: 사람만 빠지고 자리는 남는다
    assert sched[FUTURE_DAY] == (None, "confirmed")

    async with async_session() as db:
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == offboard_target["id"],
            OrgMember.organization_id == offboard_target["organization_id"],
        ))
        user = await db.get(User, offboard_target["id"])
    assert member.status == "terminated"
    assert member.termination_date == OFFBOARD_DAY
    assert member.termination_reason == "Moved out of state"
    assert member.rehire_eligible is True
    # 계정 비활성 + 자격증명 회수
    assert user.is_active is False
    assert user.clockin_pin is None
    assert user.status == "deactivated"


async def test_offboard_deletes_future_when_requested(
    async_client, admin_headers, offboard_target, test_store_id
):
    """delete: 미래 스케줄은 소프트 삭제. 과거는 그대로."""
    uid = str(offboard_target["id"])
    resp = await async_client.post(_url(uid), headers=admin_headers, json={
        "termination_date": OFFBOARD_DAY.isoformat(),
        "future_schedule_action": "delete",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["future_schedules_affected"] == 1

    sched = await _schedules(uid, test_store_id)
    assert sched[PAST_DAY] == (uid, "confirmed")
    assert sched[FUTURE_DAY] == (uid, "deleted")


async def test_offboard_requires_future_schedule_action(
    async_client, admin_headers, offboard_target
):
    """기본값이 없다 — 액션을 빠뜨리면 422."""
    resp = await async_client.post(
        _url(str(offboard_target["id"])), headers=admin_headers,
        json={"termination_date": OFFBOARD_DAY.isoformat()},
    )
    assert resp.status_code == 422, resp.text


async def test_offboard_rejects_provisional(async_client, admin_headers, test_user):
    """미가입(유령) 계정은 퇴사 개념이 없다 — 400."""
    uid = test_user["id"]
    async with async_session() as db:
        user = await db.get(User, uid)
        user.is_provisional = True
        await db.commit()
    try:
        resp = await async_client.post(_url(str(uid)), headers=admin_headers, json={
            "termination_date": OFFBOARD_DAY.isoformat(),
            "future_schedule_action": "unassign",
        })
        assert resp.status_code == 400, resp.text
    finally:
        async with async_session() as db:
            user = await db.get(User, uid)
            user.is_provisional = False
            await db.commit()


# ── lockout 방지 (§23.2) ──────────────────────────────────────────


async def test_offboard_blocks_last_admin(async_client, admin_headers, test_users):
    """조직 최고 권한을 가진 마지막 한 사람은 퇴사 처리할 수 없다.

    막지 않으면 아무도 조직을 관리할 수 없는 상태가 되고, 그 상태는 화면에서 되돌릴 수 없다.
    """
    from sqlalchemy import func

    from app.models.user import Role

    admin = test_users["testadmin"]
    async with async_session() as db:
        top = await db.scalar(
            select(func.min(Role.priority))
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(
                User.organization_id == admin["organization_id"],
                User.is_active.is_(True),
                User.is_provisional.is_(False),
            )
        )
        holders = await db.scalar(
            select(func.count())
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(
                User.organization_id == admin["organization_id"],
                User.is_active.is_(True),
                User.is_provisional.is_(False),
                Role.priority == top,
            )
        )
    if holders != 1:
        pytest.skip(f"최고 권한 보유자가 {holders}명이라 이 시나리오가 성립하지 않음")

    resp = await async_client.post(_url(str(admin["id"])), headers=admin_headers, json={
        "termination_date": OFFBOARD_DAY.isoformat(),
        "future_schedule_action": "unassign",
    })
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "LAST_ADMIN"

    # 차단됐으니 계정은 그대로여야 한다
    async with async_session() as db:
        still = await db.get(User, admin["id"])
    assert still.is_active is True
