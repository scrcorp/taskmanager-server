"""근무가능시간 프리셋(Preset) API 통합 테스트.

실제 seed DB 사용(롤백 없음) — 생성 행은 정리 픽스처로 purge.
커버: list = system + custom, custom 생성, custom 삭제(204), system 삭제 차단(400),
권한 게이트(SV/staff without grant → 403).
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.availability import StaffAvailabilityPreset
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.utils.jwt import create_access_token


async def _login(username: str) -> str:
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        return create_access_token({"sub": str(user.id), "org": str(user.organization_id)})


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 정리: preset 테이블 + availability grants purge (테스트 전후) ──
@pytest_asyncio.fixture(autouse=True)
async def _clean_presets():
    async def purge():
        async with async_session() as db:
            await db.execute(delete(StaffAvailabilityPreset))
            perm_ids = list((await db.execute(select(Permission.id).where(
                Permission.code.in_(["availability:read", "availability:manage"])))).scalars().all())
            if perm_ids:
                await db.execute(delete(RolePermission).where(RolePermission.permission_id.in_(perm_ids)))
            await db.commit()
    await purge()
    yield
    await purge()


# ── 권한 부여 (GM/SV 만; staff 는 절대 부여 안 함) ──
@pytest_asyncio.fixture
async def availability_perms(seed_roles: dict[str, UUID], _clean_presets):
    codes = ["availability:read", "availability:manage"]
    created: list[tuple[UUID, UUID]] = []
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in codes:
            p = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id
        for role_name in ("general_manager", "supervisor"):
            role_id = seed_roles[role_name]
            for code in codes:
                exists = (await db.execute(select(RolePermission).where(
                    RolePermission.role_id == role_id,
                    RolePermission.permission_id == perms[code],
                ))).scalar_one_or_none()
                if exists is None:
                    db.add(RolePermission(role_id=role_id, permission_id=perms[code]))
                    created.append((role_id, perms[code]))
        await db.commit()
    yield
    async with async_session() as db:
        for role_id, perm_id in created:
            await db.execute(delete(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == perm_id,
            ))
        await db.commit()


# ─────────────────────────── list ───────────────────────────
@pytest.mark.asyncio
async def test_list_returns_system_and_custom(async_client: AsyncClient):
    token = await _login("testadmin")  # super_owner — permission bypass
    # 처음엔 system 프리셋 5개만
    r = await async_client.get("/api/v1/console/availability/presets", headers=_h(token))
    assert r.status_code == 200, r.text
    presets = r.json()
    system = [p for p in presets if p["is_system"]]
    assert len(system) == 5
    # system 프리셋 형태 확인 (7일, id 접두사 sys-)
    full_week = next(p for p in system if p["name"] == "Full week")
    assert full_week["id"].startswith("sys-")
    assert len(full_week["days"]) == 7
    assert all(d["state"] == "full" for d in full_week["days"])
    weekend = next(p for p in system if p["name"] == "Weekends — Full")
    days = {d["day_of_week"]: d for d in weekend["days"]}
    assert days[0]["state"] == "full" and days[6]["state"] == "full"  # 0=Sun, 6=Sat
    assert days[1]["state"] == "off"

    # custom 하나 생성 후 list 에 포함
    c = await async_client.post("/api/v1/console/availability/presets", headers=_h(token),
                                json={"name": "My Custom", "days": [
                                    {"day_of_week": 2, "state": "range",
                                     "start_time": "10:00", "end_time": "16:00"}]})
    assert c.status_code == 201, c.text
    r2 = await async_client.get("/api/v1/console/availability/presets", headers=_h(token))
    body = r2.json()
    assert len([p for p in body if p["is_system"]]) == 5
    custom = [p for p in body if not p["is_system"]]
    assert len(custom) == 1 and custom[0]["name"] == "My Custom"
    cdays = {d["day_of_week"]: d for d in custom[0]["days"]}
    assert len(custom[0]["days"]) == 7  # off 채워 7일
    assert cdays[2]["state"] == "range" and cdays[2]["end_time"] == "16:00"
    assert cdays[0]["state"] == "off"


# ─────────────────────────── create ───────────────────────────
@pytest.mark.asyncio
async def test_create_custom_preset(async_client: AsyncClient):
    token = await _login("testadmin")
    r = await async_client.post("/api/v1/console/availability/presets", headers=_h(token),
                                json={"name": "Weekdays only", "days": [
                                    {"day_of_week": 1, "state": "full"},
                                    {"day_of_week": 5, "state": "full"}]})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["is_system"] is False
    assert p["name"] == "Weekdays only"
    # 유효 UUID id
    UUID(p["id"])
    days = {d["day_of_week"]: d for d in p["days"]}
    assert days[1]["state"] == "full" and days[3]["state"] == "off"


@pytest.mark.asyncio
async def test_create_duplicate_name_conflict(async_client: AsyncClient):
    token = await _login("testadmin")
    body = {"name": "Dupe", "days": [{"day_of_week": 0, "state": "full"}]}
    r1 = await async_client.post("/api/v1/console/availability/presets", headers=_h(token), json=body)
    assert r1.status_code == 201, r1.text
    r2 = await async_client.post("/api/v1/console/availability/presets", headers=_h(token), json=body)
    assert r2.status_code == 409, r2.text


# ─────────────────────────── delete ───────────────────────────
@pytest.mark.asyncio
async def test_delete_custom_ok(async_client: AsyncClient):
    token = await _login("testadmin")
    c = await async_client.post("/api/v1/console/availability/presets", headers=_h(token),
                                json={"name": "To Delete", "days": [{"day_of_week": 0, "state": "full"}]})
    assert c.status_code == 201, c.text
    pid = c.json()["id"]
    d = await async_client.delete(f"/api/v1/console/availability/presets/{pid}", headers=_h(token))
    assert d.status_code == 204, d.text
    # 사라졌는지 확인
    r = await async_client.get("/api/v1/console/availability/presets", headers=_h(token))
    assert not any(p["id"] == pid for p in r.json())


@pytest.mark.asyncio
async def test_delete_system_forbidden(async_client: AsyncClient):
    token = await _login("testadmin")
    d = await async_client.delete("/api/v1/console/availability/presets/sys-full", headers=_h(token))
    assert d.status_code == 400, d.text


@pytest.mark.asyncio
async def test_delete_missing_custom_404(async_client: AsyncClient):
    token = await _login("testadmin")
    d = await async_client.delete(
        "/api/v1/console/availability/presets/00000000-0000-0000-0000-000000000000",
        headers=_h(token))
    assert d.status_code == 404, d.text


# ─────────────────────────── 권한 게이트 ───────────────────────────
@pytest.mark.asyncio
async def test_sv_list_forbidden_without_grant(async_client: AsyncClient):
    token = await _login("testsv")  # no availability_perms
    r = await async_client.get("/api/v1/console/availability/presets", headers=_h(token))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_sv_list_ok_with_grant(async_client: AsyncClient, availability_perms):
    token = await _login("testsv")
    r = await async_client.get("/api/v1/console/availability/presets", headers=_h(token))
    assert r.status_code == 200, r.text
    assert len([p for p in r.json() if p["is_system"]]) == 5


@pytest.mark.asyncio
async def test_staff_create_forbidden(async_client: AsyncClient):
    token = await _login("teststaff")  # staff never gets availability:manage
    r = await async_client.post("/api/v1/console/availability/presets", headers=_h(token),
                                json={"name": "Nope", "days": [{"day_of_week": 0, "state": "full"}]})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_staff_delete_forbidden(async_client: AsyncClient):
    token = await _login("teststaff")
    r = await async_client.delete(
        "/api/v1/console/availability/presets/00000000-0000-0000-0000-000000000000",
        headers=_h(token))
    assert r.status_code == 403, r.text
