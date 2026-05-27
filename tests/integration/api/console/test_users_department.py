"""API integration — department (FOH/BOH) on console users endpoints.

대상:
    - POST /api/v1/console/users              (create, department 포함)
    - PUT  /api/v1/console/users/{user_id}    (set / clear department)
    - GET  /api/v1/console/users              (목록 응답에 department — _to_list_response 경로)

[작성됨]
- create with department="FOH" → 201 + body "FOH"
- create 생략 → None (미지정)
- create invalid 값 → 422
- update set "BOH" → 200 + "BOH"
- update clear (null) → None
- update invalid 값 → 422
- list 응답에 department 포함
"""
from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete

from app.database import async_session
from app.models.user import User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def cleanup_created_users() -> AsyncIterator[list[str]]:
    """테스트가 만든 user 들을 username 으로 추적 → teardown 에서 hard delete.

    시드 user(_purge_test_data)는 안 건드리는 정리 흐름이라, API 로 새로 만든
    user 는 별도로 직접 삭제해야 다음 테스트와 username 충돌이 안 난다.
    """
    usernames: list[str] = []
    yield usernames
    if usernames:
        async with async_session() as db:
            await db.execute(delete(User).where(User.username.in_(usernames)))
            await db.commit()


def _new_username() -> str:
    return f"dept_{uuid.uuid4().hex[:8]}"


async def _create_user(
    client: AsyncClient,
    headers: dict,
    staff_role_id,
    usernames: list[str],
    **extra,
):
    """staff 권한 user 생성 헬퍼. username 을 추적 리스트에 등록."""
    username = _new_username()
    usernames.append(username)
    payload = {
        "username": username,
        "password": "test1234",
        "full_name": "WC Test",
        "role_id": str(staff_role_id),
        **extra,
    }
    return await client.post("/api/v1/console/users", headers=headers, json=payload)


async def test_create_user_with_department_foh(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    resp = await _create_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        department="FOH",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["department"] == "FOH"


async def test_create_user_without_department_defaults_null(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    resp = await _create_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["department"] is None


async def test_create_user_invalid_department_returns_422(
    async_client, admin_headers, seed_roles
):
    resp = await async_client.post(
        "/api/v1/console/users",
        headers=admin_headers,
        json={
            "username": _new_username(),
            "password": "test1234",
            "full_name": "WC Test",
            "role_id": str(seed_roles["staff"]),
            "department": "kitchen",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_update_user_set_then_clear_department(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create_resp = await _create_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
    )
    assert create_resp.status_code == 201, create_resp.text
    user_id = create_resp.json()["id"]
    assert create_resp.json()["department"] is None

    # set → boh
    set_resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"department": "BOH"},
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["department"] == "BOH"

    # clear (null) → 미지정
    clear_resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"department": None},
    )
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["department"] is None


async def test_update_user_invalid_department_returns_422(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create_resp = await _create_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
    )
    user_id = create_resp.json()["id"]
    resp = await async_client.put(
        f"/api/v1/console/users/{user_id}",
        headers=admin_headers,
        json={"department": "server"},
    )
    assert resp.status_code == 422, resp.text


async def test_list_users_includes_department(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    create_resp = await _create_user(
        async_client, admin_headers, seed_roles["staff"], cleanup_created_users,
        department="FOH",
    )
    assert create_resp.status_code == 201, create_resp.text
    created_id = create_resp.json()["id"]

    list_resp = await async_client.get("/api/v1/console/users", headers=admin_headers)
    assert list_resp.status_code == 200, list_resp.text
    match = next((r for r in list_resp.json() if r["id"] == created_id), None)
    assert match is not None, "created user missing from list response"
    assert match["department"] == "FOH"


# ── PATCH /api/v1/console/users/bulk (department 일괄 변경) ──────────────


async def _created_id(client, headers, staff_role_id, usernames, **extra) -> str:
    resp = await _create_user(client, headers, staff_role_id, usernames, **extra)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_bulk_update_department_sets_value_for_all(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    """여러 직원 department 를 한 번에 BOH 로 → updated_count + 각 직원 반영."""
    id1 = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users)
    id2 = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users, department="FOH")

    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [id1, id2], "department": "BOH"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 2

    for uid in (id1, id2):
        g = await async_client.get(f"/api/v1/console/users/{uid}", headers=admin_headers)
        assert g.json()["department"] == "BOH"


async def test_bulk_update_department_clears_to_null(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    """department=null 로 일괄 → 미지정 해제."""
    uid = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users, department="FOH")

    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [uid], "department": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1

    g = await async_client.get(f"/api/v1/console/users/{uid}", headers=admin_headers)
    assert g.json()["department"] is None


async def test_bulk_update_invalid_department_returns_422(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    uid = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users)
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [uid], "department": "kitchen"},
    )
    assert resp.status_code == 422, resp.text


async def test_bulk_update_empty_user_ids_returns_422(
    async_client, admin_headers
):
    """user_ids 비어있으면 422 (min_length=1)."""
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [], "department": "FOH"},
    )
    assert resp.status_code == 422, resp.text


async def test_bulk_update_no_fields_returns_400(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    """변경 필드를 하나도 안 보내면 400 (최소 1개 필요)."""
    uid = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users)
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [uid]},
    )
    assert resp.status_code == 400, resp.text


async def test_bulk_update_is_active(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    """is_active 일괄 비활성화."""
    uid = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users)
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [uid], "is_active": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1
    g = await async_client.get(f"/api/v1/console/users/{uid}", headers=admin_headers)
    assert g.json()["is_active"] is False


async def test_bulk_update_multiple_fields_at_once(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    """department + hourly_rate 동시 일괄 적용."""
    uid = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users)
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [uid], "department": "BOH", "hourly_rate": 21.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1
    g = await async_client.get(f"/api/v1/console/users/{uid}", headers=admin_headers)
    body = g.json()
    assert body["department"] == "BOH"
    assert float(body["hourly_rate"]) == 21.5


async def test_bulk_update_disallowed_field_rejected(
    async_client, admin_headers, seed_roles, cleanup_created_users
):
    """허용되지 않은 필드(role_id 등)는 이 경로에서 거부 — 무시되지 않음.

    role_id 는 UserBulkUpdate 스키마에 없으므로 Pydantic 단계에서 그냥 무시됨
    → 그 결과 변경 필드 0개가 되어 400 (또는 다른 필드와 함께면 그 필드만 적용).
    여기선 role_id 만 보냄 → 인식 필드 0 → 400.
    """
    uid = await _created_id(async_client, admin_headers, seed_roles["staff"], cleanup_created_users)
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [uid], "role_id": str(seed_roles["supervisor"])},
    )
    assert resp.status_code == 400, resp.text


async def test_bulk_update_unauthorized_returns_403(async_client):
    """JWT 없으면 403."""
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        json={"user_ids": ["00000000-0000-0000-0000-000000000000"], "department": "FOH"},
    )
    assert resp.status_code == 403, resp.text
