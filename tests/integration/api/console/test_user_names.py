"""API integration — 구조화 이름(first/middle/last) 전환 잔여 적용 (E5/A14).

대상:
    - POST /api/v1/console/users              (구조화 이름 생성 + full_name 합성)
    - PUT  /api/v1/console/users/{user_id}    (구조화 이름 수정 + full_name 동기화,
                                               레거시 full_name-only 경로 desync 방지)

[작성됨]
- create 구조화(first/middle/last) → 저장 + full_name 합성
- create middle 생략 → full_name "First Last"
- create first 만 (last 누락) → 422 (스키마 validator)
- update 구조화 → 저장 + full_name 재합성
- update middle 빈 문자열 → middle NULL 해제 + full_name 재합성
- update first 만 보내면 → 400 (first+last 필수)
- update middle 만 보내면 → 400 (first+last 필수)
- update 레거시 full_name-only (변경) → 구조화 파트 클리어
- update 레거시 full_name-only (동일값 재전송) → 구조화 파트 유지
- update full_name 빈 문자열 → 400
- update 무관 필드(email)만 → 이름 필드 불변
"""
from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def cleanup_created_users() -> AsyncIterator[list[str]]:
    """테스트가 만든 user 들을 username 으로 추적 → teardown 에서 hard delete.

    Payroll Phase 1 이후: org_member FK(RESTRICT) 때문에 rate 이력 먼저 삭제.
    (test_users_department.py 의 정리 흐름과 동일)
    """
    from app.models.org_member import OrgMember
    from app.models.rate import HourlyRateHistory

    usernames: list[str] = []
    yield usernames
    if usernames:
        async with async_session() as db:
            user_ids = (
                await db.execute(
                    select(User.id).where(User.username.in_(usernames))
                )
            ).scalars().all()
            if user_ids:
                member_ids = (
                    await db.execute(
                        select(OrgMember.id).where(OrgMember.user_id.in_(user_ids))
                    )
                ).scalars().all()
                if member_ids:
                    await db.execute(
                        delete(HourlyRateHistory).where(
                            HourlyRateHistory.org_member_id.in_(member_ids)
                        )
                    )
            await db.execute(delete(User).where(User.username.in_(usernames)))
            await db.commit()


def _new_username() -> str:
    return f"name_{uuid.uuid4().hex[:8]}"


async def _create_structured_user(
    client: AsyncClient,
    headers: dict,
    staff_role_id,
    usernames: list[str],
    **name_fields,
):
    """구조화 이름으로 staff user 생성. username 추적 등록."""
    username = _new_username()
    usernames.append(username)
    payload = {
        "username": username,
        "password": "test1234",
        "role_id": str(staff_role_id),
        **name_fields,
    }
    return await client.post("/api/v1/console/users", headers=headers, json=payload)


async def _db_name_row(user_id: str) -> tuple:
    """DB 에서 (full_name, first, middle, last) 직접 확인 — 실제 persist 검증."""
    async with async_session() as db:
        row = (
            await db.execute(
                select(
                    User.full_name, User.first_name, User.middle_name, User.last_name
                ).where(User.id == uuid.UUID(user_id))
            )
        ).one()
    return tuple(row)


# === create ===

async def test_create_with_structured_names_persists_and_composes(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    resp = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", middle_name="Quincy", last_name="Doe",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["full_name"] == "John Quincy Doe"
    assert body["first_name"] == "John"
    assert body["middle_name"] == "Quincy"
    assert body["last_name"] == "Doe"
    assert await _db_name_row(body["id"]) == (
        "John Quincy Doe", "John", "Quincy", "Doe"
    )


async def test_create_without_middle_composes_first_last(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    resp = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="Jane", last_name="Roe",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["full_name"] == "Jane Roe"
    assert body["middle_name"] is None


async def test_create_first_without_last_returns_422(
    async_client, admin_headers, seed_roles
):
    resp = await async_client.post(
        "/api/v1/console/users",
        headers=admin_headers,
        json={
            "username": _new_username(),
            "password": "test1234",
            "role_id": str(seed_roles["staff"]),
            "first_name": "OnlyFirst",
        },
    )
    assert resp.status_code == 422, resp.text


# === update: 구조화 경로 ===

async def test_update_structured_names_syncs_full_name(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"first_name": "Johnny", "middle_name": "Q", "last_name": "Doer"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["full_name"] == "Johnny Q Doer"
    assert (body["first_name"], body["middle_name"], body["last_name"]) == (
        "Johnny", "Q", "Doer"
    )
    assert await _db_name_row(user_id) == ("Johnny Q Doer", "Johnny", "Q", "Doer")


async def test_update_empty_middle_clears_and_recomposes(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", middle_name="Quincy", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"first_name": "John", "middle_name": "", "last_name": "Doe"},
    )
    assert resp.status_code == 200, resp.text
    assert await _db_name_row(user_id) == ("John Doe", "John", None, "Doe")


async def test_update_first_only_returns_400(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"first_name": "Johnny"},
    )
    assert resp.status_code == 400, resp.text
    # 실패 시 이름 불변
    assert await _db_name_row(user_id) == ("John Doe", "John", None, "Doe")


async def test_update_middle_only_returns_400(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"middle_name": "Q"},
    )
    assert resp.status_code == 400, resp.text


# === update: 레거시 full_name-only 경로 ===

async def test_update_legacy_full_name_change_clears_structured(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"full_name": "Completely Different"},
    )
    assert resp.status_code == 200, resp.text
    # 이름이 실제로 바뀌면 낡은 구조화 파트를 비운다 (display_name desync 방지)
    assert await _db_name_row(user_id) == ("Completely Different", None, None, None)


async def test_update_legacy_same_full_name_keeps_structured(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", last_name="Doe",
    )
    user_id = create.json()["id"]

    # 동일값 재전송(no-op) — 구조화 파트 보존
    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"full_name": "John Doe"},
    )
    assert resp.status_code == 200, resp.text
    assert await _db_name_row(user_id) == ("John Doe", "John", None, "Doe")


async def test_update_empty_full_name_returns_400(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"full_name": "   "},
    )
    assert resp.status_code == 400, resp.text


async def test_update_unrelated_field_keeps_names_untouched(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create = await _create_structured_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        first_name="John", middle_name="Q", last_name="Doe",
    )
    user_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"email": "namekeep@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert await _db_name_row(user_id) == ("John Q Doe", "John", "Q", "Doe")
