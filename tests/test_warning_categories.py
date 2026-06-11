"""경고 사유 카테고리 테스트 — Warning category (v1.1).

커버: 시드(12·refusal hidden·other system) / create+slugify / 중복거부 /
soft delete + revive / system 보호 / validate_codes(legacy 허용) /
API(목록 + Owner only create·hide·delete + system 거부).
"""

from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.models.warning_category import WarningCategory
from app.services.warning_category_service import slugify_code, warning_category_service
from app.utils.exceptions import BadRequestError

BASE = "/api/v1/console/warning-categories"


async def _login(username: str) -> str:
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token({"sub": str(user.id), "org": str(user.organization_id)})


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def fresh_categories(seed_organization: dict):
    """org 카테고리를 깨끗한 기본 시드 상태로 (테스트 격리). 전후 reset."""
    org_id: UUID = seed_organization["id"]

    async def _reset() -> None:
        async with async_session() as db:
            await db.execute(
                delete(WarningCategory).where(WarningCategory.organization_id == org_id)
            )
            await db.commit()
        async with async_session() as db:
            await warning_category_service.seed_defaults(db, org_id)
            await db.commit()

    await _reset()
    yield org_id
    await _reset()


@pytest_asyncio.fixture
async def gm_warning_read(seed_roles: dict[str, UUID]) -> None:
    """general_manager 에 warnings:read 부여 (GM 이 목록은 보되 관리는 못함 확인용)."""
    async with async_session() as db:
        p = (
            await db.execute(select(Permission).where(Permission.code == "warnings:read"))
        ).scalar_one_or_none()
        if p is None:
            p = Permission(code="warnings:read", resource="warnings", action="read")
            db.add(p)
            await db.flush()
        rid = seed_roles["general_manager"]
        exists = (
            await db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == rid, RolePermission.permission_id == p.id
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            db.add(RolePermission(role_id=rid, permission_id=p.id))
        await db.commit()


# ===================================================================
# Service — seed
# ===================================================================


def test_slugify_code():
    assert slugify_code("Late Delivery!") == "late_delivery"
    assert slugify_code("  Multiple   Spaces  ") == "multiple_spaces"
    assert slugify_code("Already_ok") == "already_ok"


@pytest.mark.asyncio
async def test_seed_defaults(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        cats = await warning_category_service.list_categories(db, org_id, include_hidden=True)
    assert len(cats) == 12
    by_code = {c.code: c for c in cats}
    assert by_code["refusal_overtime"].is_hidden is True
    assert by_code["other"].is_system is True
    assert cats[-1].code == "other"  # system 맨 끝


@pytest.mark.asyncio
async def test_seed_idempotent(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        await warning_category_service.seed_defaults(db, org_id)  # 두 번째 — skip
        await db.commit()
        cats = await warning_category_service.list_categories(db, org_id)
    assert len(cats) == 12


@pytest.mark.asyncio
async def test_list_picker_excludes_hidden(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        picker = await warning_category_service.list_categories(db, org_id, include_hidden=False)
    codes = [c.code for c in picker]
    assert "refusal_overtime" not in codes  # hidden 제외
    assert "other" in codes


# ===================================================================
# Service — create / revive / 중복
# ===================================================================


@pytest.mark.asyncio
async def test_create_category_slugify(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        cat = await warning_category_service.create_category(db, org_id, "Late Delivery!")
    assert cat.code == "late_delivery"
    assert cat.label == "Late Delivery!"
    assert cat.is_system is False
    assert cat.sort_order > 110  # 기본들 뒤, system(9000) 앞


@pytest.mark.asyncio
async def test_create_duplicate_active_rejected(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        with pytest.raises(BadRequestError):
            await warning_category_service.create_category(db, org_id, "Tardiness")


@pytest.mark.asyncio
async def test_delete_then_readd_revives(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        cats = await warning_category_service.list_categories(db, org_id)
        tid = next(c for c in cats if c.code == "tardiness").id
        await warning_category_service.delete_category(db, org_id, tid)
    async with async_session() as db:
        codes = [c.code for c in await warning_category_service.list_categories(db, org_id)]
    assert "tardiness" not in codes  # 삭제 후 목록에서 빠짐
    async with async_session() as db:
        revived = await warning_category_service.create_category(db, org_id, "Tardiness")
    assert revived.id == tid  # 새 row 아님 — 기존 row revive
    assert revived.deleted_at is None
    assert revived.is_hidden is False


# ===================================================================
# Service — system 보호 / hide / validate
# ===================================================================


@pytest.mark.asyncio
async def test_system_category_cannot_be_hidden_or_deleted(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        other = next(
            c for c in await warning_category_service.list_categories(db, org_id)
            if c.code == "other"
        )
        with pytest.raises(BadRequestError):
            await warning_category_service.update_category(db, org_id, other.id, is_hidden=True)
        with pytest.raises(BadRequestError):
            await warning_category_service.delete_category(db, org_id, other.id)


@pytest.mark.asyncio
async def test_hide_toggle_and_rename(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        fighting = next(
            c for c in await warning_category_service.list_categories(db, org_id)
            if c.code == "fighting"
        )
        updated = await warning_category_service.update_category(
            db, org_id, fighting.id, label="Physical altercation", is_hidden=True
        )
    assert updated.label == "Physical altercation"
    assert updated.is_hidden is True
    assert updated.code == "fighting"  # 코드는 불변


@pytest.mark.asyncio
async def test_validate_codes(fresh_categories):
    org_id = fresh_categories
    async with async_session() as db:
        await warning_category_service.validate_codes(db, org_id, ["tardiness", "other"])
        with pytest.raises(BadRequestError):
            await warning_category_service.validate_codes(db, org_id, ["nope_not_real"])
        fid = next(
            c for c in await warning_category_service.list_categories(db, org_id)
            if c.code == "fighting"
        ).id
        await warning_category_service.delete_category(db, org_id, fid)
    async with async_session() as db:
        with pytest.raises(BadRequestError):  # 삭제된 코드 — create 검증 거부
            await warning_category_service.validate_codes(db, org_id, ["fighting"])
        # legacy(기존 경고 코드) 는 수정 시 허용
        await warning_category_service.validate_codes(
            db, org_id, ["fighting"], existing_codes=["fighting"]
        )


# ===================================================================
# API — list + Owner only 관리
# ===================================================================


@pytest.mark.asyncio
async def test_api_list_categories(async_client, fresh_categories):
    token = await _login("testadmin")  # super_owner — require_permission bypass
    resp = await async_client.get(BASE, headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 12
    assert next(c for c in data if c["code"] == "other")["is_system"] is True
    assert next(c for c in data if c["code"] == "refusal_overtime")["is_hidden"] is True
    assert data[-1]["code"] == "other"  # 맨 끝


@pytest.mark.asyncio
async def test_api_create_owner_only(async_client, fresh_categories, gm_warning_read):
    # GM (warnings:read 보유, non-owner) → 403
    gm = await _login("testgm")
    resp = await async_client.post(BASE, json={"label": "Custom Reason"}, headers=_hdr(gm))
    assert resp.status_code == 403, resp.text
    # Owner(super_owner) → 201
    owner = await _login("testadmin")
    resp = await async_client.post(BASE, json={"label": "Custom Reason"}, headers=_hdr(owner))
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "custom_reason"


@pytest.mark.asyncio
async def test_api_system_hide_delete_rejected(async_client, fresh_categories):
    owner = await _login("testadmin")
    listing = (await async_client.get(BASE, headers=_hdr(owner))).json()
    other_id = next(c["id"] for c in listing if c["code"] == "other")
    resp = await async_client.patch(
        f"{BASE}/{other_id}", json={"is_hidden": True}, headers=_hdr(owner)
    )
    assert resp.status_code == 400, resp.text
    resp = await async_client.delete(f"{BASE}/{other_id}", headers=_hdr(owner))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_api_delete_then_revive(async_client, fresh_categories):
    owner = await _login("testadmin")
    listing = (await async_client.get(BASE, headers=_hdr(owner))).json()
    tid = next(c["id"] for c in listing if c["code"] == "tardiness")
    # 삭제
    resp = await async_client.delete(f"{BASE}/{tid}", headers=_hdr(owner))
    assert resp.status_code == 200, resp.text
    after = [c["code"] for c in (await async_client.get(BASE, headers=_hdr(owner))).json()]
    assert "tardiness" not in after
    # 같은 라벨 재추가 → revive (같은 id)
    resp = await async_client.post(BASE, json={"label": "Tardiness"}, headers=_hdr(owner))
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == tid
