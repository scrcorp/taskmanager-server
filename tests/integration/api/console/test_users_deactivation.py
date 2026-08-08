"""API integration — 계정 비활성화 부수효과(자격증명 회수) + 유령 활성화 가드 + PIN 자동배정.

계약:
    - deactivate(delete/toggle/update/bulk) 시 users.clockin_pin=NULL,
      email_verified=false, status='deactivated' + org_members.clockin_pin 미러 클리어.
    - org_members 미러 회수는 **요청 org 행만** — 멀티-org(Model B) 유저의
      다른 org 소속 PIN 자격증명은 건드리지 않는다.
    - 재활성화는 status='active' 동기화만 — PIN/email_verified 는 복원하지 않는다
      (첫 로그인 인증 흐름이 처리).
    - 유령(is_provisional=true)은 is_active 변경 불가(400 provisional_account).
      delete 는 허용되며 retire 로 처리 — claim_code 회수 + is_provisional 해제
      + username 비켜주기 (삭제된 유령을 인수(가입)할 수 없어야 한다).
    - confirm-email 성공 시 PIN 미보유 유저에게 자동 배정 + org_member 미러.

테스트 유저는 API 로 생성(부수효과 포함)하고 종료 시 하드 삭제로 정리한다
(FK 전부 CASCADE/SET NULL — refresh_tokens/user_stores/org_members).
"""

from __future__ import annotations

import uuid as _uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update as sa_update

from app.core.permissions import STAFF_PRIORITY
from app.database import async_session
from app.models.org_member import OrgMember
from app.models.organization import Organization
from app.models.user import Role, User

pytestmark = pytest.mark.asyncio


# ── helpers ─────────────────────────────────────────────────────────


async def _fetch_user_state(user_id) -> dict:
    """users + org_members 미러의 검증 대상 필드 스냅샷."""
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
            "is_active": u.is_active,
            "clockin_pin": u.clockin_pin,
            "email_verified": u.email_verified,
            "status": u.status,
            "is_provisional": u.is_provisional,
            "claim_code": u.claim_code,
            "username": u.username,
            "has_member": m is not None,
            "member_pin": m.clockin_pin if m is not None else None,
        }


async def _fetch_member_pins(user_id) -> dict:
    """유저의 org_members 행들을 {organization_id: clockin_pin} 으로 스냅샷 (멀티-org 검증용)."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(OrgMember).where(OrgMember.user_id == UUID(str(user_id)))
            )
        ).scalars().all()
        return {m.organization_id: m.clockin_pin for m in rows}


async def _set_user_fields(user_id, **fields) -> None:
    """테스트 전제 상태 세팅용 직접 UPDATE (엔터티 생성이 아닌 상태 조작)."""
    async with async_session() as db:
        await db.execute(
            sa_update(User).where(User.id == UUID(str(user_id))).values(**fields)
        )
        await db.commit()


async def _clear_member_pin(user_id) -> None:
    async with async_session() as db:
        await db.execute(
            sa_update(OrgMember)
            .where(OrgMember.user_id == UUID(str(user_id)))
            .values(clockin_pin=None)
        )
        await db.commit()


async def _hard_delete_users(user_ids: list) -> None:
    """테스트가 만든 유저 정리 — 하드 삭제 (연관 행은 FK CASCADE)."""
    if not user_ids:
        return
    async with async_session() as db:
        await db.execute(
            delete(User).where(User.id.in_([UUID(str(u)) for u in user_ids]))
        )
        await db.commit()


# ── fixtures — API 로 생성, 종료 시 하드 삭제 ───────────────────────


@pytest_asyncio.fixture
async def make_user(async_client: AsyncClient, admin_headers: dict, seed_roles):
    """staff 유저를 콘솔 API 로 생성하는 팩토리 (PIN/org_member 자동 부여)."""
    created: list[str] = []

    async def _factory(*, password: str = "pw123456") -> dict:
        uname = f"deact_{_uuid.uuid4().hex[:8]}"
        resp = await async_client.post(
            "/api/v1/console/users",
            headers=admin_headers,
            json={
                "username": uname,
                "password": password,
                "first_name": "Deact",
                "last_name": uname[-4:].upper(),
                "role_id": str(seed_roles["staff"]),
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        created.append(body["id"])
        body["password"] = password
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


# ── DELETE — soft delete 시 자격증명 회수 ───────────────────────────


async def test_delete_user_revokes_credentials(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """DELETE → PIN 회수 + email_verified 해제 + status=deactivated + 미러 클리어."""
    user = await make_user()
    await _set_user_fields(user["id"], email_verified=True)
    before = await _fetch_user_state(user["id"])
    assert before["clockin_pin"] is not None  # 생성 시 자동 발급됨
    assert before["has_member"] is True
    assert before["member_pin"] == before["clockin_pin"]

    resp = await async_client.delete(
        f"/api/v1/console/users/{user['id']}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(user["id"])
    assert after["is_active"] is False
    assert after["clockin_pin"] is None
    assert after["email_verified"] is False
    assert after["status"] == "deactivated"
    assert after["member_pin"] is None


# ── 멀티-org — 미러 회수는 요청 org 행만 ────────────────────────────


async def test_deactivation_pin_mirror_scoped_to_requesting_org(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """org A 비활성화가 org B 소속의 org_members.clockin_pin 을 지우면 안 된다.

    PIN 은 org 별 독립 자격증명(uq_org_member_clockin_pin, org 스코프 unique).
    현재 API 로는 멀티-org 소속을 만들 수 없으므로(B2B 멀티-org 활성화 대기)
    두 번째 org 의 소속 행은 직접 시드한다 — 회귀 전제 상태 세팅.
    """
    user = await make_user()
    other_pin = "987654"

    # org B + org B 역할 + org B 소속(PIN 보유) 시드
    async with async_session() as db:
        org_b = Organization(name=f"deact_other_{_uuid.uuid4().hex[:6]}")
        db.add(org_b)
        await db.flush()
        role_b = Role(
            organization_id=org_b.id, name="staff", priority=STAFF_PRIORITY
        )
        db.add(role_b)
        await db.flush()
        member_b = OrgMember(
            user_id=UUID(str(user["id"])),
            organization_id=org_b.id,
            role_id=role_b.id,
            clockin_pin=other_pin,
            status="active",
        )
        db.add(member_b)
        await db.commit()
        org_b_id, role_b_id, member_b_id = org_b.id, role_b.id, member_b.id

    try:
        # org A 관리자가 비활성화
        resp = await async_client.patch(
            f"/api/v1/console/users/{user['id']}/active", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False

        pins = await _fetch_member_pins(user["id"])
        # org B 미러는 불변 — 다른 org 의 PIN 자격증명 파괴 금지
        assert pins[org_b_id] == other_pin
        # 나머지(= org A) 미러는 회수
        assert all(pin is None for org, pin in pins.items() if org != org_b_id)
    finally:
        async with async_session() as db:
            await db.execute(delete(OrgMember).where(OrgMember.id == member_b_id))
            await db.execute(delete(Role).where(Role.id == role_b_id))
            await db.execute(
                delete(Organization).where(Organization.id == org_b_id)
            )
            await db.commit()


# ── toggle_active — 비활성화 회수 / 재활성화는 status 만 ────────────


async def test_toggle_deactivate_revokes_and_reactivate_keeps_revoked(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """toggle off → 회수. 다시 on → status 만 active, PIN/email 은 회수 상태 유지."""
    user = await make_user()
    await _set_user_fields(user["id"], email_verified=True)

    # 비활성화
    resp = await async_client.patch(
        f"/api/v1/console/users/{user['id']}/active", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False
    assert resp.json()["email_verified"] is False  # 응답도 회수 후 상태

    mid = await _fetch_user_state(user["id"])
    assert mid["clockin_pin"] is None
    assert mid["email_verified"] is False
    assert mid["status"] == "deactivated"
    assert mid["member_pin"] is None

    # 재활성화 — 첫 로그인 인증 흐름이 PIN/email 을 처리하므로 여기선 복원하지 않는다
    resp = await async_client.patch(
        f"/api/v1/console/users/{user['id']}/active", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True

    after = await _fetch_user_state(user["id"])
    assert after["is_active"] is True
    assert after["status"] == "active"
    assert after["clockin_pin"] is None
    assert after["email_verified"] is False


# ── update_user (PUT) — is_active 전이 부수효과 ─────────────────────


async def test_update_user_deactivate_revokes_and_reactivate_syncs_status(
    async_client: AsyncClient, admin_headers: dict, make_user
) -> None:
    """PUT is_active=false → 회수. PUT is_active=true → status 동기화만."""
    user = await make_user()
    await _set_user_fields(user["id"], email_verified=True)

    resp = await async_client.put(
        f"/api/v1/console/users/{user['id']}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert resp.status_code == 200, resp.text

    mid = await _fetch_user_state(user["id"])
    assert mid["is_active"] is False
    assert mid["clockin_pin"] is None
    assert mid["email_verified"] is False
    assert mid["status"] == "deactivated"
    assert mid["member_pin"] is None

    resp = await async_client.put(
        f"/api/v1/console/users/{user['id']}",
        headers=admin_headers,
        json={"is_active": True},
    )
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(user["id"])
    assert after["is_active"] is True
    assert after["status"] == "active"
    assert after["clockin_pin"] is None
    assert after["email_verified"] is False


# ── 유령(미가입) 계정 가드 ──────────────────────────────────────────


async def test_ghost_toggle_active_returns_400_provisional_account(
    async_client: AsyncClient, admin_headers: dict, make_ghost
) -> None:
    """유령 toggle → 400 provisional_account (활성화는 본인 claim 으로만)."""
    ghost = await make_ghost()
    resp = await async_client.patch(
        f"/api/v1/console/users/{ghost['id']}/active", headers=admin_headers
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "provisional_account"
    assert "claim code" in detail["message"]

    # 상태 불변 — fail-closed 유지
    state = await _fetch_user_state(ghost["id"])
    assert state["is_active"] is False
    assert state["is_provisional"] is True


async def test_ghost_update_is_active_returns_400_provisional_account(
    async_client: AsyncClient, admin_headers: dict, make_ghost
) -> None:
    """유령 PUT is_active=true → 400 provisional_account."""
    ghost = await make_ghost()
    resp = await async_client.put(
        f"/api/v1/console/users/{ghost['id']}",
        headers=admin_headers,
        json={"is_active": True},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "provisional_account"


async def test_ghost_delete_retires_claim_path(
    async_client: AsyncClient, admin_headers: dict, make_ghost, seed_organization
) -> None:
    """유령 delete 는 허용(플레이스홀더 제거) — claim 경로까지 회수(retire)한다.

    소프트 삭제만 하면 claim_code 가 살아남아 '삭제된' 유령을 여전히
    인수(가입)해서 org 에 들어올 수 있다. delete 는 absorb 와 같은 retire
    패턴(claim_code 회수 + is_provisional 해제 + username 비켜주기)이어야 한다.
    """
    ghost = await make_ghost()
    before = await _fetch_user_state(ghost["id"])
    assert before["is_provisional"] is True
    assert before["clockin_pin"] is None  # 유령은 PIN 미발급
    assert before["claim_code"]

    resp = await async_client.delete(
        f"/api/v1/console/users/{ghost['id']}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(ghost["id"])
    assert after["is_active"] is False
    # retire — claim 경로 영구 차단 + 비유령 전환 + username 비켜주기
    assert after["claim_code"] is None
    assert after["is_provisional"] is False
    assert after["username"].startswith("deleted_")
    # retire 후엔 일반 비활성 계정과 동일하게 자격증명 회수 상태
    assert after["status"] == "deactivated"
    assert after["clockin_pin"] is None
    assert after["member_pin"] is None
    assert after["email_verified"] is False

    # 회수된 코드로는 더 이상 유령 조회(=가입 인수)가 안 된다
    from app.services.auth_service import auth_service

    async with async_session() as db:
        found = await auth_service.find_provisional_by_claim_code(
            db, seed_organization["id"], before["claim_code"]
        )
    assert found is None


# ── bulk — is_active 는 비유령에게만, 비활성화 시 회수 ──────────────


async def test_bulk_deactivate_clears_credentials_for_non_ghosts_only(
    async_client: AsyncClient, admin_headers: dict, make_user, make_ghost
) -> None:
    """bulk is_active=false → 비유령만 적용+회수, 유령은 완전 불변."""
    user = await make_user()
    ghost = await make_ghost()
    await _set_user_fields(user["id"], email_verified=True)

    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [user["id"], ghost["id"]], "is_active": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1  # 유령 제외

    u = await _fetch_user_state(user["id"])
    assert u["is_active"] is False
    assert u["clockin_pin"] is None
    assert u["email_verified"] is False
    assert u["status"] == "deactivated"
    assert u["member_pin"] is None

    g = await _fetch_user_state(ghost["id"])
    assert g["is_provisional"] is True
    assert g["status"] == "active"  # 유령은 부수효과 미적용
    assert g["claim_code"]


async def test_bulk_activate_does_not_touch_ghost(
    async_client: AsyncClient, admin_headers: dict, make_ghost
) -> None:
    """bulk is_active=true 로도 유령은 활성화되지 않는다 (fail-closed)."""
    ghost = await make_ghost()
    resp = await async_client.patch(
        "/api/v1/console/users/bulk",
        headers=admin_headers,
        json={"user_ids": [ghost["id"]], "is_active": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 0

    state = await _fetch_user_state(ghost["id"])
    assert state["is_active"] is False


# ── confirm-email — PIN 자동 배정 ───────────────────────────────────


def _require_test_code() -> str:
    from app.config import settings

    code = (settings.EMAIL_VERIFICATION_TEST_CODE or "").strip()
    if not code:
        pytest.skip("EMAIL_VERIFICATION_TEST_CODE not set")
    return code


async def _app_login_token(
    async_client: AsyncClient, username: str, password: str
) -> str:
    resp = await async_client.post(
        "/api/v1/app/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_confirm_email_assigns_pin_when_missing(
    async_client: AsyncClient, make_user
) -> None:
    """PIN 미보유 유저의 이메일 인증 성공 → 4~6자리 PIN 자동 배정 + 미러."""
    code = _require_test_code()
    user = await make_user()
    # PIN 미보유 상태로 (users + org_members 미러 모두)
    await _set_user_fields(user["id"], clockin_pin=None)
    await _clear_member_pin(user["id"])

    token = await _app_login_token(
        async_client, user["username"], user["password"]
    )
    resp = await async_client.post(
        "/api/v1/app/auth/confirm-email",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": f"{user['username']}@test.com", "code": code},
    )
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(user["id"])
    assert after["email_verified"] is True
    assert after["clockin_pin"] is not None
    assert after["clockin_pin"].isdigit()
    assert 4 <= len(after["clockin_pin"]) <= 6
    assert after["member_pin"] == after["clockin_pin"]


async def test_confirm_email_keeps_existing_pin(
    async_client: AsyncClient, make_user
) -> None:
    """이미 PIN 을 가진 유저의 이메일 인증 → PIN 은 그대로."""
    code = _require_test_code()
    user = await make_user()
    before = await _fetch_user_state(user["id"])
    assert before["clockin_pin"] is not None

    token = await _app_login_token(
        async_client, user["username"], user["password"]
    )
    resp = await async_client.post(
        "/api/v1/app/auth/confirm-email",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": f"{user['username']}@test.com", "code": code},
    )
    assert resp.status_code == 200, resp.text

    after = await _fetch_user_state(user["id"])
    assert after["email_verified"] is True
    assert after["clockin_pin"] == before["clockin_pin"]
    assert after["member_pin"] == before["member_pin"]
