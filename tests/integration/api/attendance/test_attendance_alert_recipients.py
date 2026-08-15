"""Integration — 근태 알림 수신자 범위.

조기 출근 강행/겹침 출근/근태 수정 알림은 원래 `schedules:update` **권한**으로
수신자를 골랐다. 그 권한은 SV 기본 세트에 들어 있는 데다 매장 스코프도 없어서,
결과적으로 조직 전체의 SV 까지 **전 매장** 근태 알림을 in-app 과 email 양쪽으로
받았다. 잡음이 쌓이면 정작 조치해야 할 사람이 알림을 흘려보낸다.

새 규칙은 OR 두 갈래다:
  1. 그 매장에 manager 로 체크된 사람 — 콘솔 Staff > Detail 의 매장별 manager
     체크(`user_stores.is_manager`)가 그대로 기준. **GM 도 SV 도 이 경로로만
     들어온다** (그 매장 GM 만 받는다).
  2. Owner / Super Owner — 오너는 오너라서 받는다. 매장 배정과 무관하게 항상.

이 파일은 그 두 갈래와 경계를 고정한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.alert import Alert
from app.models.attendance import Attendance
from app.models.user import User
from app.models.user_store import UserStore
from app.services.alert_service import alert_service

pytestmark = pytest.mark.asyncio


async def _set_membership(
    user_id: UUID, store_id: UUID, *, is_manager: bool | None
) -> None:
    """user_stores 배정을 원하는 상태로 맞춘다. is_manager=None 이면 배정 제거."""
    async with async_session() as db:
        row = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id
            )
        )
        if is_manager is None:
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(
                UserStore(user_id=user_id, store_id=store_id, is_manager=is_manager)
            )
        else:
            row.is_manager = is_manager
        await db.commit()


async def _recipient_ids(
    org_id: UUID, store_id: UUID, exclude_user_id: UUID | None = None
) -> set[UUID]:
    async with async_session() as db:
        rows = await db.execute(
            alert_service.attendance_recipient_query(
                organization_id=org_id,
                store_id=store_id,
                exclude_user_id=exclude_user_id,
            )
        )
        return {row[0] for row in rows.all()}


@pytest_asyncio.fixture
async def gm_unassigned(test_users: dict, test_store_id: UUID, second_store_id: UUID):
    """GM 을 어느 매장에도 배정하지 않은 상태로 만든다."""
    gm_id = test_users["testgm"]["id"]
    await _set_membership(gm_id, test_store_id, is_manager=None)
    await _set_membership(gm_id, second_store_id, is_manager=None)
    yield gm_id
    await _set_membership(gm_id, test_store_id, is_manager=True)


# ── 규칙 1: GM 도 매장 manager 체크가 있어야 받는다 ─────────


async def test_gm_without_manager_check_is_excluded(
    gm_unassigned: UUID, seed_organization: dict, test_store_id: UUID
) -> None:
    """GM 이라도 그 매장 manager 로 체크돼 있지 않으면 안 받는다.

    직급만으로 들어오면 조직 전체 GM 이 전 매장 알림을 받는 원래 증상이 재발한다.
    """
    assert gm_unassigned not in await _recipient_ids(
        seed_organization["id"], test_store_id
    )


async def test_gm_receives_only_for_stores_they_manage(
    test_users: dict,
    seed_organization: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    """GM 은 자기가 manager 로 체크된 매장 알림만 받는다 — 다른 매장 건은 안 받는다."""
    gm_id = test_users["testgm"]["id"]
    org_id = seed_organization["id"]

    await _set_membership(gm_id, test_store_id, is_manager=True)
    await _set_membership(gm_id, second_store_id, is_manager=None)
    try:
        assert gm_id in await _recipient_ids(org_id, test_store_id)
        assert gm_id not in await _recipient_ids(org_id, second_store_id)
    finally:
        await _set_membership(gm_id, test_store_id, is_manager=True)


async def test_gm_assigned_but_not_manager_is_excluded(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """매장에 소속만 되고 manager 체크가 꺼져 있으면 GM 도 안 받는다.

    수신 여부를 가르는 건 소속이 아니라 **체크박스**라는 걸 못박는다.
    """
    gm_id = test_users["testgm"]["id"]
    await _set_membership(gm_id, test_store_id, is_manager=False)
    try:
        assert gm_id not in await _recipient_ids(
            seed_organization["id"], test_store_id
        )
    finally:
        await _set_membership(gm_id, test_store_id, is_manager=True)


# ── 규칙 2: Owner 는 매장과 무관하게 항상 ───────────────────


async def test_owner_tier_receives_regardless_of_store_assignment(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """Owner / Super Owner 는 매장 배정이 없어도 받는다.

    매장 조건을 join 으로 걸면 `user_stores` 행이 없는 Owner 가 통째로 빠지므로,
    그 실수를 여기서 잡는다.
    """
    admin_id = test_users["testadmin"]["id"]  # super_owner
    await _set_membership(admin_id, test_store_id, is_manager=None)
    try:
        assert admin_id in await _recipient_ids(
            seed_organization["id"], test_store_id
        )
    finally:
        await _set_membership(admin_id, test_store_id, is_manager=None)


# ── SV / Staff 도 같은 기준 ─────────────────────────────────


async def test_sv_receives_only_when_checked_as_store_manager(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """SV 는 그 매장 manager 로 체크됐을 때만 받는다.

    원래 증상(스케줄도 안 짜는 SV 가 전 매장 알림 수신)의 직접 방지선.
    """
    sv_id = test_users["testsv"]["id"]
    org_id = seed_organization["id"]

    await _set_membership(sv_id, test_store_id, is_manager=False)
    assert sv_id not in await _recipient_ids(org_id, test_store_id)

    await _set_membership(sv_id, test_store_id, is_manager=True)
    try:
        assert sv_id in await _recipient_ids(org_id, test_store_id)
    finally:
        await _set_membership(sv_id, test_store_id, is_manager=None)


async def test_sv_manager_of_another_store_is_excluded(
    test_users: dict,
    seed_organization: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    """다른 매장의 manager 인 SV 는 이 매장 알림을 안 받는다 — 매장 스코프가 산다."""
    sv_id = test_users["testsv"]["id"]

    await _set_membership(sv_id, second_store_id, is_manager=True)
    await _set_membership(sv_id, test_store_id, is_manager=None)
    try:
        assert sv_id not in await _recipient_ids(
            seed_organization["id"], test_store_id
        )
        assert sv_id in await _recipient_ids(
            seed_organization["id"], second_store_id
        )
    finally:
        await _set_membership(sv_id, second_store_id, is_manager=None)


async def test_staff_member_of_store_is_excluded(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """매장 소속이어도 manager 체크가 없으면 안 받는다 — 체크박스가 곧 기준."""
    staff_id = test_users["teststaff"]["id"]
    await _set_membership(staff_id, test_store_id, is_manager=False)

    assert staff_id not in await _recipient_ids(
        seed_organization["id"], test_store_id
    )


# ── 공통 가드 ───────────────────────────────────────────────


async def test_recipients_exclude_self(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """exclude_user_id 는 본인을 뺀다 — 자기 행위를 자기에게 알리지 않는다."""
    gm_id = test_users["testgm"]["id"]
    await _set_membership(gm_id, test_store_id, is_manager=True)

    assert gm_id in await _recipient_ids(seed_organization["id"], test_store_id)
    assert gm_id not in await _recipient_ids(
        seed_organization["id"], test_store_id, exclude_user_id=gm_id
    )


async def test_recipients_exclude_inactive_and_deleted(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """비활성/소프트 삭제된 사람은 제외 — 조치할 수 없는 사람에게 보내지 않는다."""
    gm_id = test_users["testgm"]["id"]
    org_id = seed_organization["id"]
    await _set_membership(gm_id, test_store_id, is_manager=True)

    for field, disabled in (
        ("is_active", False),
        ("deleted_at", datetime.now(timezone.utc)),
    ):
        async with async_session() as db:
            setattr(await db.get(User, gm_id), field, disabled)
            await db.commit()
        try:
            assert gm_id not in await _recipient_ids(org_id, test_store_id)
        finally:
            async with async_session() as db:
                setattr(
                    await db.get(User, gm_id),
                    field,
                    True if field == "is_active" else None,
                )
                await db.commit()

    # 원복 후 다시 수신자여야 한다 (정리 누락 시 뒤 테스트가 조용히 통과하는 걸 방지)
    assert gm_id in await _recipient_ids(org_id, test_store_id)


async def test_recipients_are_unique_per_user(
    test_users: dict, seed_organization: dict, test_store_id: UUID
) -> None:
    """한 사람당 한 행 — Owner 이면서 매장 manager 여도 알림이 두 번 가면 안 된다."""
    admin_id = test_users["testadmin"]["id"]
    await _set_membership(admin_id, test_store_id, is_manager=True)  # 두 갈래 동시 충족

    async with async_session() as db:
        rows = (
            await db.execute(
                alert_service.attendance_recipient_query(
                    organization_id=seed_organization["id"], store_id=test_store_id
                )
            )
        ).all()
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids))


# ── end-to-end: 실제 조기 출근 강행 ─────────────────────────


async def test_early_clock_in_alerts_reach_store_managers_and_owner(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_users: dict,
    test_store_id: UUID,
    make_schedule,
) -> None:
    """조기 출근을 사유와 함께 강행 → 그 매장 manager 와 Owner 만 받는다.

    쿼리 단위 테스트만으로는 "서비스가 그 쿼리를 실제로 쓰는가" 를 못 잡는다 —
    원래 버그가 바로 그 배선(파라미터로 받은 store_id 를 쿼리에서 안 씀)이었다.
    """
    from app.utils.timezone import get_store_day_config

    await _set_membership(test_user["id"], test_store_id, is_manager=False)
    await _set_membership(test_users["testgm"]["id"], test_store_id, is_manager=True)
    # SV 는 이 매장에 배정하되 manager 체크는 끈다 → 받으면 안 된다.
    await _set_membership(test_users["testsv"]["id"], test_store_id, is_manager=False)

    async with async_session() as db:
        tz_name, _ = await get_store_day_config(db, test_store_id)

    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    start_at = (local_now + timedelta(minutes=120)).replace(
        second=0, microsecond=0, tzinfo=None
    )
    await make_schedule(
        test_user, start_at=start_at, end_at=start_at + timedelta(minutes=240)
    )

    res = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "reason": "Asked to come in early",
        },
    )
    assert res.status_code == 200, res.text

    # 이번 출근 건의 알림만 본다 — DB 에 과거 알림이 남아 있으면 전수 조회는
    # 수정 전 데이터를 집어와 항상 실패한다.
    async with async_session() as db:
        attendance_id = await db.scalar(
            select(Attendance.id)
            .where(Attendance.user_id == test_user["id"])
            .order_by(Attendance.created_at.desc())
            .limit(1)
        )
        recipients = {
            row[0]
            for row in (
                await db.execute(
                    select(Alert.user_id).where(
                        Alert.type == "early_clock_in_override",
                        Alert.reference_type == "attendance",
                        Alert.reference_id == attendance_id,
                    )
                )
            ).all()
        }

    assert test_users["testgm"]["id"] in recipients, "매장 manager 인 GM 은 받아야 한다"
    assert test_users["testadmin"]["id"] in recipients, "Owner 는 항상 받아야 한다"
    assert test_users["testsv"]["id"] not in recipients
    assert test_users["teststaff"]["id"] not in recipients
