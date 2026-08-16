"""콘솔 진입 권한(D12)과 role rank 동률(D13).

D12 — 콘솔 로그인 게이트는 priority 비교가 아니라 `console:access` 권한으로 판정한다.
       priority 로 막으면 "이 매장만 staff 에게 연다"(§18) 같은 조정이 구조적으로 불가능하다.
D13 — 같은 org 에 같은 priority 인 role 을 여럿 둘 수 있다. "같은 단계, 다른 권한"의 전제.
"""
from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.permissions import CONSOLE_ACCESS
from app.database import async_session
from app.models.permission import Permission, RolePermission
from app.models.user import Role

pytestmark = pytest.mark.asyncio

LOGIN_URL = "/api/v1/console/auth/login"


# ── D12 ──────────────────────────────────────────────────────────


async def test_sv_can_sign_in_with_console_access(async_client, test_users):
    """SV 는 기본으로 console:access 를 갖는다 — 전환 전과 동일한 동작."""
    resp = await async_client.post(
        LOGIN_URL, json={"username": "testsv", "password": "1234"}
    )
    assert resp.status_code == 200, resp.text


async def test_staff_cannot_sign_in(async_client, test_user):
    """staff 는 console:access 가 없어 진입 불가."""
    resp = await async_client.post(
        LOGIN_URL, json={"username": "teststaff", "password": "1234"}
    )
    assert resp.status_code == 403, resp.text


@pytest_asyncio.fixture
async def sv_without_console_access(test_users) -> AsyncIterator[None]:
    """testsv 의 role 에서 console:access 만 회수한다 (테스트 후 복구)."""
    sv_role_id = None
    async with async_session() as db:
        perm = await db.scalar(select(Permission).where(Permission.code == CONSOLE_ACCESS))
        assert perm is not None, "console:access 권한 행이 있어야 한다 (마이그레이션 261609d1aeae)"
        role = await db.scalar(
            select(Role).join(
                # testsv 의 role 을 이름으로 찾는다
                Role.users
            ).where(Role.name == "supervisor")
        )
        sv_role_id = role.id
        await db.execute(delete(RolePermission).where(
            RolePermission.role_id == sv_role_id,
            RolePermission.permission_id == perm.id,
        ))
        await db.commit()
    try:
        yield None
    finally:
        async with async_session() as db:
            perm = await db.scalar(select(Permission).where(Permission.code == CONSOLE_ACCESS))
            exists = await db.scalar(select(RolePermission).where(
                RolePermission.role_id == sv_role_id,
                RolePermission.permission_id == perm.id,
            ))
            if exists is None:
                db.add(RolePermission(role_id=sv_role_id, permission_id=perm.id))
                await db.commit()


async def test_revoking_console_access_blocks_sign_in(
    async_client, sv_without_console_access
):
    """권한을 회수하면 priority 와 무관하게 막힌다 — 게이트가 실제로 권한 기반이라는 증거."""
    resp = await async_client.post(
        LOGIN_URL, json={"username": "testsv", "password": "1234"}
    )
    assert resp.status_code == 403, resp.text


# ── D13 ──────────────────────────────────────────────────────────


async def test_duplicate_role_priority_allowed(test_user):
    """같은 org 에 같은 priority 인 role 을 두 개 만들 수 있다 (uq 제거 확인)."""
    org_id = test_user["organization_id"]
    made: list[uuid.UUID] = []
    try:
        async with async_session() as db:
            existing = await db.scalar(
                select(Role).where(Role.organization_id == org_id, Role.name == "supervisor")
            )
            assert existing is not None
            for name in ("__test_kitchen_lead__", "__test_floor_lead__"):
                role = Role(
                    organization_id=org_id, name=name, priority=existing.priority
                )
                db.add(role)
                await db.flush()
                made.append(role.id)
            await db.commit()

        async with async_session() as db:
            rows = (await db.execute(select(Role).where(Role.id.in_(made)))).scalars().all()
        assert len(rows) == 2
        assert rows[0].priority == rows[1].priority
    finally:
        async with async_session() as db:
            await db.execute(delete(Role).where(Role.id.in_(made)))
            await db.commit()


async def test_duplicate_role_name_still_rejected(test_user):
    """이름 중복은 여전히 막힌다 — 완화한 것은 priority 뿐이다."""
    from app.repositories.role_repository import role_repository

    org_id = test_user["organization_id"]
    async with async_session() as db:
        # 이름 중복 → True
        assert await role_repository.check_duplicate(db, org_id, "supervisor", 999) is True
        # priority 만 겹치는 새 이름 → False (D13 으로 완화된 부분)
        sv = await db.scalar(
            select(Role).where(Role.organization_id == org_id, Role.name == "supervisor")
        )
        assert await role_repository.check_duplicate(
            db, org_id, "__brand_new_role__", sv.priority
        ) is False
