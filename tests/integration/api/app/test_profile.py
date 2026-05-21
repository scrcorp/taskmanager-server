"""API integration tests — app/api/app/profile.py 의 clockin_pin endpoints.

[작성됨] — 이번 phase
- GET  /api/v1/app/profile/clockin-pin
    · 기존 PIN 있을 때 그대로 반환
    · PIN 이 None 일 때 lazy generate
    · JWT 없으면 403
- POST /api/v1/app/profile/clockin-pin/regenerate
    · 새 값 반환 (기존과 다름, 6자리)
    · JWT 없으면 403
- PUT  /api/v1/app/profile/clockin-pin
    · 정상 update 200
    · 자기 자신의 기존 PIN 으로 update (no-op-like) 200
    · 중복 PIN → 409 'Not available'
    · 형식 위반 → 422
    · JWT 없으면 403

[작성 필요] — 추후
- GET  /api/v1/app/profile                (내 프로필 조회)
- PUT  /api/v1/app/profile                (프로필 update)
- GET  /api/v1/app/profile/alert-preferences
- PUT  /api/v1/app/profile/alert-preferences
- (기타 app/profile.py 의 모든 router 함수)
"""

from __future__ import annotations

import re
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app


pytestmark = pytest.mark.asyncio


# ── teststaff JWT fixture (staff 권한 app 로그인) ──────────────────


@pytest_asyncio.fixture(scope="session")
async def staff_token() -> str:
    """teststaff 로 app/auth/login → access_token 세션 캐시."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/app/auth/login",
            json={"username": "teststaff", "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def staff_headers(staff_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {staff_token}"}


# ── GET /profile/clockin-pin ─────────────────────────────────────────


async def test_get_my_clockin_pin_returns_existing(
    async_client: AsyncClient,
    staff_headers: dict,
    test_user: dict,
) -> None:
    """PIN 이 이미 있을 때 그대로 반환 (lazy generate 안 일어남)."""
    resp = await async_client.get(
        "/api/v1/app/profile/clockin-pin", headers=staff_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == test_user["clockin_pin"]
    assert resp.json()["user_id"] == str(test_user["id"])


async def test_get_my_clockin_pin_lazy_generates_when_none(
    async_client: AsyncClient,
    staff_headers: dict,
    test_user: dict,
    db: AsyncSession,
    restore_pins,
) -> None:
    """PIN 이 None 일 때 자동으로 6자리 생성 후 반환."""
    # PIN 을 NULL 로 만듦
    await db.execute(
        text("UPDATE users SET clockin_pin=NULL WHERE id=:id"),
        {"id": str(test_user["id"])},
    )
    await db.commit()

    resp = await async_client.get(
        "/api/v1/app/profile/clockin-pin", headers=staff_headers
    )
    assert resp.status_code == 200, resp.text
    new_pin = resp.json()["clockin_pin"]
    assert new_pin is not None
    assert re.fullmatch(r"\d{6}", new_pin) is not None


async def test_get_my_clockin_pin_unauthorized(
    async_client: AsyncClient,
) -> None:
    """JWT 없으면 401."""
    resp = await async_client.get("/api/v1/app/profile/clockin-pin")
    assert resp.status_code == 403, resp.text  # FastAPI HTTPBearer 기본: 403


# ── POST /profile/clockin-pin/regenerate ────────────────────────────


async def test_regenerate_my_clockin_pin_returns_new_value(
    async_client: AsyncClient,
    staff_headers: dict,
    test_user: dict,
    restore_pins,
) -> None:
    """regenerate → 기존 PIN 과 다른 6자리 새 값 반환."""
    original = test_user["clockin_pin"]

    resp = await async_client.post(
        "/api/v1/app/profile/clockin-pin/regenerate", headers=staff_headers
    )
    assert resp.status_code == 200, resp.text
    new_pin = resp.json()["clockin_pin"]
    assert new_pin != original
    assert re.fullmatch(r"\d{6}", new_pin) is not None


async def test_regenerate_my_clockin_pin_unauthorized(
    async_client: AsyncClient,
) -> None:
    """JWT 없으면 401."""
    resp = await async_client.post("/api/v1/app/profile/clockin-pin/regenerate")
    assert resp.status_code == 403, resp.text  # FastAPI HTTPBearer 기본: 403


# ── PUT /profile/clockin-pin ────────────────────────────────────────


async def test_update_my_clockin_pin_success(
    async_client: AsyncClient,
    staff_headers: dict,
    test_user: dict,
    test_users: dict,
    restore_pins,
) -> None:
    """본인이 자기 PIN 을 충돌 없는 값으로 update → 200."""
    other_pins = {info["clockin_pin"] for info in test_users.values()}
    new_pin = "200000"
    while new_pin in other_pins:
        new_pin = f"{int(new_pin) + 1:06d}"

    resp = await async_client.put(
        "/api/v1/app/profile/clockin-pin",
        headers=staff_headers,
        json={"clockin_pin": new_pin},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == new_pin


async def test_update_my_clockin_pin_same_as_self_succeeds(
    async_client: AsyncClient,
    staff_headers: dict,
    test_user: dict,
    restore_pins,
) -> None:
    """자기 자신의 기존 PIN 으로 update — 자기 자신과의 충돌은 unique 위반 아님 → 200."""
    same_pin = test_user["clockin_pin"]

    resp = await async_client.put(
        "/api/v1/app/profile/clockin-pin",
        headers=staff_headers,
        json={"clockin_pin": same_pin},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == same_pin


async def test_update_my_clockin_pin_duplicate_returns_409(
    async_client: AsyncClient,
    staff_headers: dict,
    test_users: dict,
    restore_pins,
) -> None:
    """본인 PIN 을 다른 user 와 같은 값으로 update → 409 'Not available'."""
    conflicting_pin = test_users["testadmin"]["clockin_pin"]

    resp = await async_client.put(
        "/api/v1/app/profile/clockin-pin",
        headers=staff_headers,
        json={"clockin_pin": conflicting_pin},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Not available"


async def test_update_my_clockin_pin_invalid_format_returns_422(
    async_client: AsyncClient,
    staff_headers: dict,
) -> None:
    """PIN 형식 위반 (6자리 숫자 아님) → 422."""
    resp = await async_client.put(
        "/api/v1/app/profile/clockin-pin",
        headers=staff_headers,
        json={"clockin_pin": "12"},  # 너무 짧음
    )
    assert resp.status_code == 422, resp.text


async def test_update_my_clockin_pin_unauthorized(
    async_client: AsyncClient,
) -> None:
    """JWT 없으면 401."""
    resp = await async_client.put(
        "/api/v1/app/profile/clockin-pin", json={"clockin_pin": "123456"}
    )
    assert resp.status_code == 403, resp.text  # FastAPI HTTPBearer 기본: 403
