"""Integration — POST /attendance/store-managers (조기 출근 사유의 "누가 불렀나" 명단).

이 엔드포인트의 위험은 기능이 아니라 **노출 범위**다. 조건 하나가 조용히 빠지면
키오스크 앞에서 PIN 하나로 전 직원 명단이나 퇴사자 이름이 뽑힌다. 그래서 여기서
검증하는 것은 대부분 "무엇이 안 나오는가" 다.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.user import Role, User
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio


async def _ensure_user_store(
    user_id: UUID, store_id: UUID, *, is_manager: bool = False
) -> None:
    async with async_session() as db:
        existing = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id
            )
        )
        if existing is None:
            db.add(
                UserStore(
                    user_id=user_id, store_id=store_id, is_manager=is_manager
                )
            )
            await db.commit()


async def _drop_user_store(user_id: UUID, store_id: UUID) -> None:
    async with async_session() as db:
        row = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id
            )
        )
        if row is not None:
            await db.delete(row)
            await db.commit()


@pytest_asyncio.fixture
async def staff_asking(test_user: dict, test_store_id: UUID) -> dict:
    """명단을 조회하는 직원(staff) — 이 매장 소속."""
    await _ensure_user_store(test_user["id"], test_store_id)
    return test_user


@pytest_asyncio.fixture
async def gm_here(test_users: dict, test_store_id: UUID) -> dict:
    await _ensure_user_store(test_users["testgm"]["id"], test_store_id, is_manager=True)
    return test_users["testgm"]


@pytest_asyncio.fixture
async def sv_here(test_users: dict, test_store_id: UUID) -> dict:
    """SV 는 `is_manager=False`(근무 배정만) 로 묶는다.

    포함 규칙을 `is_manager=True` 로 좁히지 않는 이유를 고정하는 픽스처다 —
    실제로 "일찍 와 달라" 고 부르는 일이 잦은 쪽이 이 SV 이고, 빠지면 전부
    "직접 입력" 으로 새어나가 집계가 비어버린다.
    """
    await _ensure_user_store(test_users["testsv"]["id"], test_store_id, is_manager=False)
    return test_users["testsv"]


@pytest_asyncio.fixture
async def temp_users(seed_organization: dict, test_store_id: UUID):
    """이 파일 전용 임시 유저 — 세션 공용 4명을 오염시키지 않기 위해 직접 만든다.

    - owner_no_store : Owner. `user_stores` 행이 **없다**(전 매장 관리 규칙 확인용)
    - inactive_sv    : 이 매장 소속 SV 지만 비활성(퇴사 직전 상태)
    - other_store_gm : 다른 매장에만 소속된 GM
    """
    from app.services.attendance_device_service import generate_clockin_pin

    org_id: UUID = seed_organization["id"]
    created: list[UUID] = []

    async with async_session() as db:
        roles = {
            r.name: r.id
            for r in (
                await db.execute(select(Role).where(Role.organization_id == org_id))
            ).scalars().all()
        }
        specs = [
            ("__t6_owner_no_store", "Zed Owner", "owner", True),
            ("__t6_inactive_sv", "Gone SV", "supervisor", False),
            ("__t6_other_store_gm", "Elsewhere GM", "general_manager", True),
            ("__t6_nameless_gm", "   ", "general_manager", True),
        ]
        made: dict[str, User] = {}
        for username, full_name, role_name, active in specs:
            u = User(
                organization_id=org_id,
                role_id=roles[role_name],
                username=username,
                full_name=full_name,
                password_hash="x",
                clockin_pin=generate_clockin_pin(),
                is_active=active,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            made[username] = u
            created.append(u.id)

    # 비활성 SV 는 이 매장 소속이지만 목록에 나오면 안 된다.
    await _ensure_user_store(made["__t6_inactive_sv"].id, test_store_id)
    # 이름 없는 GM 도 이 매장 소속 — 제외 사유가 "이름 없음" 하나임을 분명히 한다.
    await _ensure_user_store(made["__t6_nameless_gm"].id, test_store_id)

    yield {
        "owner_no_store": made["__t6_owner_no_store"],
        "inactive_sv": made["__t6_inactive_sv"],
        "other_store_gm": made["__t6_other_store_gm"],
        "nameless_gm": made["__t6_nameless_gm"],
    }

    async with async_session() as db:
        for uid in created:
            for us in (
                await db.execute(select(UserStore).where(UserStore.user_id == uid))
            ).scalars().all():
                await db.delete(us)
            row = await db.get(User, uid)
            if row is not None:
                await db.delete(row)
        await db.commit()


async def _fetch(
    async_client: AsyncClient, headers: dict, user: dict
) -> list[dict]:
    res = await async_client.post(
        "/api/v1/attendance/store-managers",
        headers=headers,
        json={"user_id": str(user["id"]), "pin": user["clockin_pin"]},
    )
    assert res.status_code == 200, res.text
    return res.json()


async def test_lists_managers_and_supervisors_of_this_store(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
    gm_here: dict,
    sv_here: dict,
) -> None:
    """GM + SV 가 나오고, 응답 필드는 넷뿐이다."""
    rows = await _fetch(async_client, device_auth_headers, staff_asking)

    ids = {r["user_id"] for r in rows}
    assert str(gm_here["id"]) in ids
    assert str(sv_here["id"]) in ids
    for r in rows:
        # 연락처·PIN·시급이 실리면 키오스크 화면(고객 눈에도 띈다)에서 새어나간다.
        assert set(r) == {"user_id", "full_name", "role_name", "role_priority"}


async def test_staff_and_super_owner_are_not_listed(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
    test_users: dict,
    gm_here: dict,
) -> None:
    """전 직원 명단이 아니다 — staff 는 제외. Super Owner 는 매장 운영을 하지 않는다."""
    rows = await _fetch(async_client, device_auth_headers, staff_asking)
    ids = {r["user_id"] for r in rows}

    assert str(test_users["teststaff"]["id"]) not in ids
    assert str(test_users["testadmin"]["id"]) not in ids


async def test_asking_user_is_excluded_even_when_supervisor(
    async_client: AsyncClient,
    device_auth_headers: dict,
    sv_here: dict,
    gm_here: dict,
) -> None:
    """'누가 불렀나' 에 자기 자신은 답이 아니다 — SV 본인이 물어도 자기는 안 나온다."""
    rows = await _fetch(async_client, device_auth_headers, sv_here)
    ids = {r["user_id"] for r in rows}

    assert str(sv_here["id"]) not in ids
    assert str(gm_here["id"]) in ids


async def test_inactive_user_is_excluded(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
    temp_users: dict,
) -> None:
    """비활성/퇴사자는 부를 수 없는 사람이다. 이름도 내려가면 안 된다."""
    rows = await _fetch(async_client, device_auth_headers, staff_asking)
    ids = {r["user_id"] for r in rows}

    assert str(temp_users["inactive_sv"].id) not in ids


async def test_nameless_account_is_not_listed_by_username(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
    temp_users: dict,
) -> None:
    """표시할 이름이 없으면 **목록에서 뺀다** — username 으로 폴백하지 않는다.

    폴백하면 키오스크 화면에 로그인 계정명(`admin` 등)이 그대로 뜬다. 이 엔드포인트가
    PIN 게이트까지 두는 이유(매니저 명단 = 사회공학 표적)와 정면으로 어긋나고,
    직원 입장에서도 계정명으로는 그 사람을 알아볼 수 없다.
    """
    rows = await _fetch(async_client, device_auth_headers, staff_asking)

    assert str(temp_users["nameless_gm"].id) not in {r["user_id"] for r in rows}
    assert all(r["full_name"] != "__t6_nameless_gm" for r in rows)
    assert all(r["full_name"].strip() for r in rows), "빈 이름이 실리면 안 된다"


async def test_other_store_manager_is_excluded_but_owner_is_included(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
    temp_users: dict,
    second_store_id: UUID,
) -> None:
    """매장 스코프 — 다른 매장 GM 은 빠진다. 단 Owner 는 행이 없어도 포함.

    Owner 를 빼면 "실제로 오너가 불렀다" 는 흔한 케이스가 전부 '직접 입력' 으로
    새어나가 이 컬럼을 만든 이유가 사라진다.
    """
    await _ensure_user_store(temp_users["other_store_gm"].id, second_store_id)

    rows = await _fetch(async_client, device_auth_headers, staff_asking)
    ids = {r["user_id"] for r in rows}

    assert str(temp_users["other_store_gm"].id) not in ids
    assert str(temp_users["owner_no_store"].id) in ids


async def test_sorted_by_role_then_name(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
    gm_here: dict,
    sv_here: dict,
    temp_users: dict,
) -> None:
    """role_priority 오름차순 → 이름 오름차순. Owner(10) < GM(20) < SV(30)."""
    rows = await _fetch(async_client, device_auth_headers, staff_asking)
    priorities = [r["role_priority"] for r in rows]

    assert priorities == sorted(priorities)
    names_by_prio: dict[int, list[str]] = {}
    for r in rows:
        names_by_prio.setdefault(r["role_priority"], []).append(r["full_name"])
    for names in names_by_prio.values():
        # 대소문자 취급은 DB collation 소관이라 파이썬 기본 정렬(ASCII)과 다르다.
        # 여기서 고정하려는 건 "이름 순으로 나온다" 이지 collation 자체가 아니다.
        assert names == sorted(names, key=str.casefold)


async def test_wrong_pin_is_rejected(
    async_client: AsyncClient,
    device_auth_headers: dict,
    staff_asking: dict,
) -> None:
    """PIN 게이트가 실제로 걸린다 — device token 만으로는 명단을 못 뽑는다."""
    bad = "9" if staff_asking["clockin_pin"][0] != "9" else "1"
    res = await async_client.post(
        "/api/v1/attendance/store-managers",
        headers=device_auth_headers,
        json={
            "user_id": str(staff_asking["id"]),
            "pin": bad + staff_asking["clockin_pin"][1:],
        },
    )
    assert res.status_code == 400, res.text


async def test_requires_device_token(
    async_client: AsyncClient, staff_asking: dict
) -> None:
    """device 인증 없이는 401."""
    res = await async_client.post(
        "/api/v1/attendance/store-managers",
        json={"user_id": str(staff_asking["id"]), "pin": staff_asking["clockin_pin"]},
    )
    assert res.status_code == 401, res.text


async def test_device_without_store_is_rejected_with_code(
    async_client: AsyncClient,
    unassigned_device_token: str,
    staff_asking: dict,
) -> None:
    """매장 미할당 기기 — 어느 매장의 명단인지 정할 수 없다."""
    res = await async_client.post(
        "/api/v1/attendance/store-managers",
        headers={"Authorization": f"Bearer {unassigned_device_token}"},
        json={"user_id": str(staff_asking["id"]), "pin": staff_asking["clockin_pin"]},
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"]["code"] == "DEVICE_NO_STORE"
