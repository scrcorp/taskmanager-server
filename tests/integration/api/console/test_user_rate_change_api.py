"""API integration — rate-change 콘솔 엔드포인트 (Payroll v1 Phase 1).

대상:
    - POST /api/v1/console/users/{user_id}/rate-changes  (record_rate_change 경유)
    - GET  /api/v1/console/users/{user_id}/rate-changes  (이력 목록, 최신 우선)

검증:
    - 즉시 적용(effective_date 생략/오늘) vs 미래 적용(이력만, 컬럼 미반영)
    - 같은 값 재등록 no-op (recorded=False, 이력 안 늘어남)
    - new_rate ≤ 0 → 400 / effective_date 형식 오류 → 422 / 없는 유저 → 404
    - 권한: staff(permission 없음)·SV(permission 있어도 cost 게이트) → 403,
      GM(permission + cost 가시성) → 허용
    - 목록 정렬: effective_date DESC, created_at DESC
    - permission 시드: payroll:* 는 Owner 기본 전용 (GM/SV/Staff 제외)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from app.core.permissions import (
    ALL_PERMISSION_CODES,
    DEFAULT_ROLE_PERMISSIONS,
)
from app.database import async_session
from app.models.org_member import OrgMember
from app.models.permission import Permission, RolePermission
from app.models.rate import HourlyRateHistory
from app.models.user import User

pytestmark = pytest.mark.asyncio

PAYROLL_CODES = ("payroll:read", "payroll:confirm", "payroll:export")
USERS_PERM_CODES = ("users:read", "users:update")


def _today():
    return datetime.now(timezone.utc).date()


async def _login(username: str) -> dict[str, str]:
    """username → Authorization 헤더 (토큰 직접 mint — multi-org login 의존 끊기)."""
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        token = create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )
    return {"Authorization": f"Bearer {token}"}


async def _member_id(user_id: UUID, org_id: UUID) -> UUID:
    async with async_session() as db:
        return (
            await db.execute(
                select(OrgMember.id).where(
                    OrgMember.user_id == user_id,
                    OrgMember.organization_id == org_id,
                )
            )
        ).scalar_one()


async def _history_count(member_id: UUID) -> int:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(HourlyRateHistory).where(
                    HourlyRateHistory.org_member_id == member_id
                )
            )
        ).scalars().all()
        return len(rows)


async def _column_rates(user_id: UUID, org_id: UUID) -> tuple[Decimal | None, Decimal | None]:
    """(org_members.hourly_rate, users.hourly_rate)."""
    async with async_session() as db:
        member_rate = await db.scalar(
            select(OrgMember.hourly_rate).where(
                OrgMember.user_id == user_id,
                OrgMember.organization_id == org_id,
            )
        )
        users_rate = await db.scalar(
            select(User.hourly_rate).where(User.id == user_id)
        )
        return (
            Decimal(member_rate) if member_rate is not None else None,
            Decimal(users_rate) if users_rate is not None else None,
        )


@pytest_asyncio.fixture
async def _restore_rates(test_users: dict):
    """teststaff 의 이력/컬럼 rate 를 테스트 전후 초기화."""
    uid = test_users["teststaff"]["id"]

    async def _cleanup() -> None:
        async with async_session() as db:
            member_ids = (
                await db.execute(
                    select(OrgMember.id).where(OrgMember.user_id == uid)
                )
            ).scalars().all()
            await db.execute(
                delete(HourlyRateHistory).where(
                    HourlyRateHistory.org_member_id.in_(member_ids)
                )
            )
            await db.execute(
                update(OrgMember)
                .where(OrgMember.id.in_(member_ids))
                .values(hourly_rate=None)
            )
            await db.execute(
                update(User).where(User.id == uid).values(hourly_rate=None)
            )
            await db.commit()

    await _cleanup()
    yield
    await _cleanup()


@pytest_asyncio.fixture
async def users_perms_gm_sv(seed_roles: dict[str, UUID]):
    """users:read/update 를 GM·SV role 에 임시 부여.

    - GM: permission + cost 가시성 → 허용 경로 검증
    - SV: permission 은 있지만 cost 게이트(GM 미만) → 403 분기 검증
    teardown 에서 이 fixture 가 새로 넣은 (role, permission) 쌍만 제거.
    """
    added: list[tuple[UUID, UUID]] = []
    async with async_session() as db:
        perm_ids: dict[str, UUID] = {}
        for code in USERS_PERM_CODES:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perm_ids[code] = p.id

        for role_name in ("general_manager", "supervisor"):
            role_id = seed_roles[role_name]
            for code in USERS_PERM_CODES:
                exists = (
                    await db.execute(
                        select(RolePermission).where(
                            RolePermission.role_id == role_id,
                            RolePermission.permission_id == perm_ids[code],
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    db.add(
                        RolePermission(role_id=role_id, permission_id=perm_ids[code])
                    )
                    added.append((role_id, perm_ids[code]))
        await db.commit()

    yield

    if added:
        async with async_session() as db:
            for role_id, perm_id in added:
                await db.execute(
                    delete(RolePermission).where(
                        RolePermission.role_id == role_id,
                        RolePermission.permission_id == perm_id,
                    )
                )
            await db.commit()


def _url(user_id) -> str:
    return f"/api/v1/console/users/{user_id}/rate-changes"


# ---------------------------------------------------------------------------
# Permission 시드 — payroll:* 는 Owner 기본 전용
# ---------------------------------------------------------------------------


async def test_payroll_codes_registered_owner_only() -> None:
    """payroll:read/confirm/export 가 REGISTRY 에 있고 기본 부여는 Owner 만."""
    for code in PAYROLL_CODES:
        assert code in ALL_PERMISSION_CODES, code
        assert code in DEFAULT_ROLE_PERMISSIONS["owner"], code
        assert code in DEFAULT_ROLE_PERMISSIONS["super_owner"], code
        assert code not in DEFAULT_ROLE_PERMISSIONS["gm"], code
        assert code not in DEFAULT_ROLE_PERMISSIONS["sv"], code
        assert code not in DEFAULT_ROLE_PERMISSIONS["staff"], code

    # GM/SV/Staff 기본엔 payroll 리소스 자체가 없어야 한다 (미래 코드 추가 가드)
    for role in ("gm", "sv", "staff"):
        assert not any(
            p.startswith("payroll:") for p in DEFAULT_ROLE_PERMISSIONS[role]
        ), role


# ---------------------------------------------------------------------------
# POST — 등록 (즉시/미래/no-op/검증)
# ---------------------------------------------------------------------------


async def test_create_rate_change_immediate(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    test_users: dict,
    _restore_rates,
) -> None:
    """effective_date 생략 = 오늘 즉시 적용 — 이력 + 컬럼 dual-write + 응답 필드."""
    resp = await async_client.post(
        _url(test_user["id"]),
        headers=admin_headers,
        json={"new_rate": 21.5, "reason": "Annual raise"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] is True
    entry = body["entry"]
    assert entry["new_rate"] == 21.5
    assert entry["old_rate"] is None  # 최초 기록
    assert entry["effective_date"] == _today().isoformat()
    assert entry["applied"] is True
    assert entry["reason"] == "Annual raise"
    assert entry["changed_by"] == str(test_users["testadmin"]["id"])
    assert entry["changed_by_name"] == test_users["testadmin"]["full_name"]

    member_rate, users_rate = await _column_rates(
        test_user["id"], test_user["organization_id"]
    )
    assert member_rate == Decimal("21.50")  # canonical
    assert users_rate == Decimal("21.50")  # 전환기 미러

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_count(member_id) == 1


async def test_create_rate_change_future_not_applied(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """미래 적용일 — 이력만 기록(applied=False), 컬럼은 일일 잡 전까지 미반영."""
    future = _today() + timedelta(days=7)
    resp = await async_client.post(
        _url(test_user["id"]),
        headers=admin_headers,
        json={"new_rate": 30, "effective_date": future.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] is True
    assert body["entry"]["applied"] is False
    assert body["entry"]["effective_date"] == future.isoformat()

    member_rate, users_rate = await _column_rates(
        test_user["id"], test_user["organization_id"]
    )
    assert member_rate is None  # 아직 미반영
    assert users_rate is None

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_count(member_id) == 1


async def test_create_same_value_noop(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """다른 날 같은 값 재등록 → recorded=False, 이력 안 늘어남 (노이즈 방지)."""
    yesterday = _today() - timedelta(days=1)
    resp = await async_client.post(
        _url(test_user["id"]),
        headers=admin_headers,
        json={"new_rate": 21.5, "effective_date": yesterday.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] is True

    # 오늘 같은 값 재등록 (콘솔 폼 전체 재전송 시나리오) — no-op
    resp = await async_client.post(
        _url(test_user["id"]),
        headers=admin_headers,
        json={"new_rate": 21.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] is False
    assert resp.json()["entry"] is None  # no-op 응답

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_count(member_id) == 1


async def test_create_same_day_updates_existing_row(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """같은 날 다른 값 재등록 → 새 행이 아니라 기존 행 update (UNIQUE 계약)."""
    for value in (21.5, 23.0):
        resp = await async_client.post(
            _url(test_user["id"]),
            headers=admin_headers,
            json={"new_rate": value},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["recorded"] is True

    assert resp.json()["entry"]["new_rate"] == 23.0
    assert resp.json()["entry"]["old_rate"] is None  # 첫 기록의 old(NULL) 보존
    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_count(member_id) == 1

    member_rate, users_rate = await _column_rates(
        test_user["id"], test_user["organization_id"]
    )
    assert member_rate == Decimal("23.00")
    assert users_rate == Decimal("23.00")


async def test_create_rate_zero_or_negative_400(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """0/음수 시급 → 400 (actionable 메시지, 이력 없음)."""
    for bad in (0, -5):
        resp = await async_client.post(
            _url(test_user["id"]),
            headers=admin_headers,
            json={"new_rate": bad},
        )
        assert resp.status_code == 400, resp.text
        assert "greater than 0" in resp.json()["detail"]

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_count(member_id) == 0


async def test_create_invalid_effective_date_422(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """effective_date 형식 오류 → 422 (pydantic 검증)."""
    resp = await async_client.post(
        _url(test_user["id"]),
        headers=admin_headers,
        json={"new_rate": 20, "effective_date": "not-a-date"},
    )
    assert resp.status_code == 422, resp.text


async def test_create_unknown_user_404(
    async_client: AsyncClient,
    admin_headers: dict,
) -> None:
    """조직에 없는 유저 → 404."""
    resp = await async_client.post(
        _url("00000000-0000-0000-0000-000000000000"),
        headers=admin_headers,
        json={"new_rate": 20},
    )
    assert resp.status_code == 404, resp.text


async def test_create_legacy_user_without_member_404(
    async_client: AsyncClient,
    admin_headers: dict,
    seed_organization: dict,
    seed_roles: dict,
    _restore_rates,
) -> None:
    """org_member 미생성(레거시) 계정 — 이력 불가라 404, GET 은 빈 목록."""
    async with async_session() as db:
        legacy = User(
            organization_id=seed_organization["id"],
            role_id=seed_roles["staff"],
            username="ratechange_legacy_user",
            full_name="Rate Legacy",
            password_hash="x",
            is_active=True,
        )
        db.add(legacy)
        await db.commit()
        await db.refresh(legacy)
        legacy_id = legacy.id

    try:
        resp = await async_client.post(
            _url(legacy_id), headers=admin_headers, json={"new_rate": 20}
        )
        assert resp.status_code == 404, resp.text
        assert "membership" in resp.json()["detail"].lower()

        resp = await async_client.get(_url(legacy_id), headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == []
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == legacy_id))
            await db.commit()


# ---------------------------------------------------------------------------
# 권한 — staff(permission 없음) / SV(cost 게이트) / GM(허용)
# ---------------------------------------------------------------------------


async def test_staff_denied_both_endpoints(
    async_client: AsyncClient,
    test_user: dict,
    test_users: dict,
    _restore_rates,
) -> None:
    """staff — users:update/users:read permission 자체가 없어 403."""
    headers = await _login("teststaff")
    resp = await async_client.post(
        _url(test_user["id"]), headers=headers, json={"new_rate": 20}
    )
    assert resp.status_code == 403, resp.text

    resp = await async_client.get(_url(test_user["id"]), headers=headers)
    assert resp.status_code == 403, resp.text


async def test_sv_denied_by_cost_gate(
    async_client: AsyncClient,
    test_user: dict,
    users_perms_gm_sv,
    _restore_rates,
) -> None:
    """SV — users:read/update 를 부여받아도 cost 가시성(GM+) 게이트로 403."""
    headers = await _login("testsv")
    resp = await async_client.post(
        _url(test_user["id"]), headers=headers, json={"new_rate": 20}
    )
    assert resp.status_code == 403, resp.text
    assert "GM and above" in resp.json()["detail"]

    resp = await async_client.get(_url(test_user["id"]), headers=headers)
    assert resp.status_code == 403, resp.text
    assert "GM and above" in resp.json()["detail"]

    member_id = await _member_id(test_user["id"], test_user["organization_id"])
    assert await _history_count(member_id) == 0  # 거부 경로에서 기록 없음


async def test_gm_allowed(
    async_client: AsyncClient,
    test_user: dict,
    test_users: dict,
    users_perms_gm_sv,
    _restore_rates,
) -> None:
    """GM — users:update + cost 가시성 → 등록/조회 허용, changed_by=GM."""
    headers = await _login("testgm")
    resp = await async_client.post(
        _url(test_user["id"]),
        headers=headers,
        json={"new_rate": 18.75, "reason": "GM adjustment"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entry"]["changed_by"] == str(test_users["testgm"]["id"])

    resp = await async_client.get(_url(test_user["id"]), headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert resp.json()[0]["changed_by_name"] == test_users["testgm"]["full_name"]


# ---------------------------------------------------------------------------
# GET — 이력 목록 정렬 (최신 우선)
# ---------------------------------------------------------------------------


async def test_history_list_newest_first(
    async_client: AsyncClient,
    admin_headers: dict,
    test_user: dict,
    _restore_rates,
) -> None:
    """effective_date DESC 정렬 — 미래 > 오늘 > 과거 순으로 반환."""
    today = _today()
    dates = [today - timedelta(days=5), today + timedelta(days=3), today]
    for i, eff in enumerate(dates):
        resp = await async_client.post(
            _url(test_user["id"]),
            headers=admin_headers,
            json={
                "new_rate": 20 + i,
                "effective_date": eff.isoformat(),
                "reason": f"step {i}",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["recorded"] is True

    resp = await async_client.get(_url(test_user["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert [e["effective_date"] for e in entries] == [
        (today + timedelta(days=3)).isoformat(),
        today.isoformat(),
        (today - timedelta(days=5)).isoformat(),
    ]
    # applied 플래그 — 미래만 False
    assert [e["applied"] for e in entries] == [False, True, True]
