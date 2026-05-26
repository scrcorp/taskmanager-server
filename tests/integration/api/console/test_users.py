"""API integration tests — app/api/console/users.py.

대상:
    - PUT  /api/v1/console/users/{user_id}/clockin-pin           (관리자 PIN 직접 지정)
    - POST /api/v1/console/users/{user_id}/clockin-pin/regenerate (관리자 PIN 재발급)

[작성됨] — 이번 phase
- update PIN 정상
- update PIN 중복 → 409 'Not available'
- update PIN 형식 위반 → 422
- update PIN 존재하지 않는 user → 404
- update PIN 자기 자신과 같은 값 (no-op-like) → 200
- update PIN 권한 부족 (token 없음) → 403
- regenerate 후 org 내 unique 보장

[작성 필요] — 추후
- GET  /api/v1/console/users/{user_id}/clockin-pin   (이미 test_attendance_device_admin.py 에 있음 — 점진 마이그레이션 대상)
- POST /api/v1/console/users  (create user, role/email/full_name validation 분기)
- PUT  /api/v1/console/users/{user_id}  (update 일반 필드)
- DELETE /api/v1/console/users/{user_id}  (soft delete)
- POST /api/v1/console/users/{user_id}/reset-password
- POST /api/v1/console/users/{user_id}/stores/{store_id}  (store 배정)
- (기타 users.py 의 모든 router 함수)
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


# ── PUT /clockin-pin (관리자 PIN 직접 지정) ──────────────────────────


async def test_admin_update_user_clockin_pin_success(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_users: dict,
    restore_pins,
) -> None:
    """admin 이 직원 PIN 을 충돌 없는 값으로 지정 → 200, 응답에 새 PIN."""
    other_pins = {info["clockin_pin"] for info in test_users.values()}
    new_pin = "100000"
    while new_pin in other_pins:
        new_pin = f"{int(new_pin) + 1:06d}"

    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin",
        headers=admin_headers,
        json={"clockin_pin": new_pin},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == new_pin
    assert resp.json()["user_id"] == str(test_user["id"])


async def test_admin_update_user_clockin_pin_duplicate_returns_409(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_users: dict,
    restore_pins,
) -> None:
    """다른 user 와 같은 PIN 으로 update 시도 → 409 + detail 'Not available'."""
    conflicting_pin = test_users["testadmin"]["clockin_pin"]

    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin",
        headers=admin_headers,
        json={"clockin_pin": conflicting_pin},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Not available"


async def test_admin_update_user_clockin_pin_same_as_self_succeeds(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    restore_pins,
) -> None:
    """자기 자신의 기존 PIN 으로 update — 자기 자신과의 충돌은 unique 위반 아님 → 200."""
    same_pin = test_user["clockin_pin"]

    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin",
        headers=admin_headers,
        json={"clockin_pin": same_pin},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == same_pin


async def test_admin_update_user_clockin_pin_invalid_format_returns_422(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
) -> None:
    """PIN 형식 위반 (6자리 숫자 아님) → 422."""
    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin",
        headers=admin_headers,
        json={"clockin_pin": "abc"},
    )
    assert resp.status_code == 422, resp.text


async def test_admin_update_user_clockin_pin_unknown_user_returns_404(
    async_client: AsyncClient,
    admin_headers: dict,
) -> None:
    """존재하지 않는 user id → 404."""
    bogus_user_id = "00000000-0000-0000-0000-000000000000"
    resp = await async_client.put(
        f"/api/v1/console/users/{bogus_user_id}/clockin-pin",
        headers=admin_headers,
        json={"clockin_pin": "123456"},
    )
    assert resp.status_code == 404, resp.text


async def test_admin_update_user_clockin_pin_unauthorized(
    async_client: AsyncClient,
    test_user: dict,
) -> None:
    """JWT 없으면 403 (FastAPI HTTPBearer 기본 응답)."""
    resp = await async_client.put(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin",
        json={"clockin_pin": "123456"},
    )
    assert resp.status_code == 403, resp.text


# ── POST /clockin-pin/regenerate ─────────────────────────────────────


async def test_admin_regenerate_user_clockin_pin_stays_unique_in_org(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_users: dict,
    restore_pins,
) -> None:
    """regenerate 후 같은 org 내에서 PIN unique 보장 (commit_pin_or_409 가 보호)."""
    resp = await async_client.post(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin/regenerate",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    new_pin = resp.json()["clockin_pin"]

    other_pins = {
        info["clockin_pin"]
        for username, info in test_users.items()
        if username != "teststaff"
    }
    assert new_pin not in other_pins, (
        f"regenerate 가 같은 org 의 다른 user 와 같은 PIN 을 만듦: {new_pin}"
    )


async def test_admin_regenerate_user_clockin_pin_unauthorized(
    async_client: AsyncClient,
    test_user: dict,
) -> None:
    """JWT 없으면 403."""
    resp = await async_client.post(
        f"/api/v1/console/users/{test_user['id']}/clockin-pin/regenerate",
    )
    assert resp.status_code == 403, resp.text
