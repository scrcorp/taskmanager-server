"""API integration — 관리자 수동 이메일 인증 (POST /console/users/{id}/verify-email).

계약:
    - 성공 시 email_verified=True + PIN 미보유면 clockin_pin 자동 배정
      (org_members 미러 포함) — confirm-email 과 동일한 finalize 경로.
    - 이메일 소유 확인이 없는 관리자 대행 경로 — 가드가 대신 지킨다:
        * 유령(is_provisional) → 400 provisional_account
        * 비활성 계정 → 400 (재활성화 먼저)
        * 이미 인증됨 → 400
        * 이메일 없음 → 400
        * 같은 이메일을 이미 인증한 다른 계정 존재 → 409
    - users:update 권한 필요 (staff → 403).

테스트 유저는 API 로 생성하고 종료 시 하드 삭제로 정리한다.
"""

from __future__ import annotations

import uuid as _uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update as sa_update

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.user import User

pytestmark = pytest.mark.asyncio


# ── helpers ─────────────────────────────────────────────────────────


async def _fetch_user_state(user_id) -> dict:
    async with async_session() as db:
        u = (
            await db.execute(select(User).where(User.id == UUID(str(user_id))))
        ).scalar_one()
        m = (
            await db.execute(
                select(OrgMember).where(OrgMember.user_id == u.id)
            )
        ).scalar_one_or_none()
        return {
            "email": u.email,
            "email_verified": u.email_verified,
            "clockin_pin": u.clockin_pin,
            "member_pin": m.clockin_pin if m is not None else None,
        }


async def _set_user_fields(user_id, **fields) -> None:
    """테스트 전제 상태 세팅용 직접 UPDATE (엔터티 생성이 아닌 상태 조작)."""
    async with async_session() as db:
        await db.execute(
            sa_update(User).where(User.id == UUID(str(user_id))).values(**fields)
        )
        await db.commit()


async def _clear_pins(user_id) -> None:
    async with async_session() as db:
        await db.execute(
            sa_update(User)
            .where(User.id == UUID(str(user_id)))
            .values(clockin_pin=None)
        )
        await db.execute(
            sa_update(OrgMember)
            .where(OrgMember.user_id == UUID(str(user_id)))
            .values(clockin_pin=None)
        )
        await db.commit()


async def _hard_delete_users(user_ids: list) -> None:
    if not user_ids:
        return
    async with async_session() as db:
        await db.execute(
            delete(User).where(User.id.in_([UUID(str(u)) for u in user_ids]))
        )
        await db.commit()


def _verify_url(user_id) -> str:
    return f"/api/v1/console/users/{user_id}/verify-email"


# ── fixtures ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def make_user(async_client: AsyncClient, admin_headers: dict, seed_roles):
    """staff 유저를 콘솔 API 로 생성하는 팩토리 (PIN/org_member 자동 부여)."""
    created: list[str] = []

    async def _factory() -> dict:
        uname = f"mverify_{_uuid.uuid4().hex[:8]}"
        resp = await async_client.post(
            "/api/v1/console/users",
            headers=admin_headers,
            json={
                "username": uname,
                "password": "pw123456",
                "first_name": "Verify",
                "last_name": uname[-4:].upper(),
                "role_id": str(seed_roles["staff"]),
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        created.append(body["id"])
        return body

    yield _factory
    await _hard_delete_users(created)


@pytest_asyncio.fixture
async def make_ghost(
    async_client: AsyncClient, admin_headers: dict, seed_roles, test_store_id
):
    """미가입(유령) 직원을 콘솔 API 로 생성하는 팩토리."""
    created: list[str] = []

    async def _factory() -> dict:
        resp = await async_client.post(
            "/api/v1/console/users/provisional",
            headers=admin_headers,
            json={
                "full_name": f"Ghost {_uuid.uuid4().hex[:6]}",
                "role_id": str(seed_roles["staff"]),
                "store_ids": [str(test_store_id)],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        created.append(body["id"])
        return body

    yield _factory
    await _hard_delete_users(created)


# ── 성공 경로 ────────────────────────────────────────────────────────


async def test_verify_email_success(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """이메일 보유 + 미인증 → 200, email_verified=True, 기존 PIN 유지."""
    user = await make_user()
    email = f"verify_{_uuid.uuid4().hex[:8]}@example.com"
    await _set_user_fields(user["id"], email=email)
    before = await _fetch_user_state(user["id"])
    assert before["email_verified"] is False
    assert before["clockin_pin"] is not None  # 콘솔 생성 시 자동 발급

    resp = await async_client.post(_verify_url(user["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_verified"] is True

    after = await _fetch_user_state(user["id"])
    assert after["email_verified"] is True
    assert after["clockin_pin"] == before["clockin_pin"]  # 재발급 없음
    assert after["member_pin"] == before["clockin_pin"]


async def test_verify_email_assigns_pin_when_missing(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """PIN 미보유 유저 → 인증과 함께 PIN 자동 배정 + org_members 미러."""
    user = await make_user()
    email = f"verify_{_uuid.uuid4().hex[:8]}@example.com"
    await _set_user_fields(user["id"], email=email)
    await _clear_pins(user["id"])

    resp = await async_client.post(_verify_url(user["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(user["id"])
    assert after["email_verified"] is True
    assert after["clockin_pin"] is not None
    assert after["member_pin"] == after["clockin_pin"]


async def test_verify_email_normalizes_case(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """대문자 이메일은 소문자로 정규화되어 확정된다."""
    user = await make_user()
    local = f"Verify_{_uuid.uuid4().hex[:8]}"
    await _set_user_fields(user["id"], email=f"{local}@Example.COM")

    resp = await async_client.post(_verify_url(user["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(user["id"])
    assert after["email"] == f"{local.lower()}@example.com"
    assert after["email_verified"] is True


# ── 가드 ────────────────────────────────────────────────────────────


async def test_verify_email_already_verified(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    user = await make_user()
    email = f"verify_{_uuid.uuid4().hex[:8]}@example.com"
    await _set_user_fields(user["id"], email=email, email_verified=True)

    resp = await async_client.post(_verify_url(user["id"]), headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "already verified" in resp.text


async def test_verify_email_without_email(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """이메일이 없는 유저 → 400 (이메일 먼저 등록하라는 안내)."""
    user = await make_user()  # 콘솔 생성 유저는 email 없음

    resp = await async_client.post(_verify_url(user["id"]), headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "no email" in resp.text


async def test_verify_email_duplicate_conflict(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """같은 이메일을 이미 인증한 다른 계정이 있으면 409."""
    email = f"verify_{_uuid.uuid4().hex[:8]}@example.com"
    owner = await make_user()
    await _set_user_fields(owner["id"], email=email, email_verified=True)
    other = await make_user()
    await _set_user_fields(other["id"], email=email)

    resp = await async_client.post(_verify_url(other["id"]), headers=admin_headers)
    assert resp.status_code == 409, resp.text
    assert "already used by another account" in resp.text

    after = await _fetch_user_state(other["id"])
    assert after["email_verified"] is False


async def test_verify_email_provisional_blocked(
    async_client: AsyncClient, admin_headers: dict, make_ghost
) -> None:
    """유령(미가입) 계정 → 400 provisional_account."""
    ghost = await make_ghost()

    resp = await async_client.post(_verify_url(ghost["id"]), headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "provisional_account" in resp.text


async def test_verify_email_inactive_blocked(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """비활성 계정 → 400 (재활성화 먼저). 자격증명 회수 상태를 존중한다."""
    user = await make_user()
    email = f"verify_{_uuid.uuid4().hex[:8]}@example.com"
    await _set_user_fields(user["id"], email=email, is_active=False)

    resp = await async_client.post(_verify_url(user["id"]), headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "deactivated" in resp.text

    after = await _fetch_user_state(user["id"])
    assert after["email_verified"] is False


async def test_verify_email_user_not_found(
    async_client: AsyncClient, admin_headers: dict
) -> None:
    resp = await async_client.post(
        _verify_url(_uuid.uuid4()), headers=admin_headers
    )
    assert resp.status_code == 404, resp.text


# ── 권한 ────────────────────────────────────────────────────────────


async def test_verify_email_requires_users_update(
    async_client: AsyncClient, make_user, test_users
) -> None:
    """users:update 권한이 없는 staff 토큰 → 403."""
    user = await make_user()
    login = await async_client.post(
        "/api/v1/app/auth/login",
        json={"username": "teststaff", "password": "1234"},
    )
    assert login.status_code == 200, login.text
    staff_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await async_client.post(_verify_url(user["id"]), headers=staff_headers)
    assert resp.status_code == 403, resp.text
