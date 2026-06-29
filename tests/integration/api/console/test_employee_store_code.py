"""API + service tests — Phase 2-A: stores.code / users.employee_no 배선.

대상:
    - PUT /api/v1/console/stores/{id}  (code 반영 + 대문자/길이 validator + org 유일 409)
    - PUT /api/v1/console/users/{id}   (employee_no 반영 + validator + org 유일 409)
    - user_service.update_user         ('기존 사번 변경'은 Owner 만 — owner gate)
"""
from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient

from app.database import async_session
from app.repositories.user_repository import user_repository
from app.schemas.user import UserUpdate
from app.services.user_service import user_service
from app.utils.exceptions import ForbiddenError

pytestmark = pytest.mark.asyncio

STORE_BASE = "/api/v1/console/stores"
USER_BASE = "/api/v1/console/users"


# ── stores.code ─────────────────────────────────────────────


async def _set_store_code(client, headers, store_id, code):
    return await client.put(f"{STORE_BASE}/{store_id}", headers=headers, json={"code": code})


async def test_store_update_code_reflected_uppercased(
    async_client: AsyncClient, admin_headers: dict, test_store_id: UUID
):
    """code 설정 → 대문자로 정규화되어 응답/상세에 반영."""
    try:
        resp = await _set_store_code(async_client, admin_headers, test_store_id, "qx7")
        assert resp.status_code == 200, resp.text
        assert resp.json()["code"] == "QX7"
        detail = await async_client.get(f"{STORE_BASE}/{test_store_id}", headers=admin_headers)
        assert detail.json()["code"] == "QX7"
    finally:
        await _set_store_code(async_client, admin_headers, test_store_id, None)


async def test_store_code_invalid_returns_422(
    async_client: AsyncClient, admin_headers: dict, test_store_id: UUID
):
    """2~10 영숫자 위반 → 422 (validator). 11자 초과 / 특수문자."""
    too_long = await _set_store_code(async_client, admin_headers, test_store_id, "ELEVENCHARS")  # 11자
    assert too_long.status_code == 422, too_long.text
    bad_char = await _set_store_code(async_client, admin_headers, test_store_id, "AB-CD")  # 특수문자
    assert bad_char.status_code == 422, bad_char.text


async def test_store_code_ten_chars_allowed(
    async_client: AsyncClient, admin_headers: dict, test_store_id: UUID
):
    """현장 관행(이름 약어)을 흡수하기 위해 최대 10자 허용."""
    try:
        resp = await _set_store_code(async_client, admin_headers, test_store_id, "swcafe1234")  # 10자
        assert resp.status_code == 200, resp.text
        assert resp.json()["code"] == "SWCAFE1234"
    finally:
        await _set_store_code(async_client, admin_headers, test_store_id, None)


async def test_store_duplicate_code_returns_409(
    async_client: AsyncClient, admin_headers: dict, test_store_id: UUID, second_store_id: UUID
):
    """같은 org 내 동일 코드 → 409."""
    try:
        r1 = await _set_store_code(async_client, admin_headers, test_store_id, "dup")
        assert r1.status_code == 200, r1.text
        r2 = await _set_store_code(async_client, admin_headers, second_store_id, "dup")
        assert r2.status_code == 409, r2.text
    finally:
        await _set_store_code(async_client, admin_headers, test_store_id, None)
        await _set_store_code(async_client, admin_headers, second_store_id, None)


# ── users.employee_no ───────────────────────────────────────


async def _set_emp(client, headers, user_id, emp):
    return await client.put(f"{USER_BASE}/{user_id}", headers=headers, json={"employee_no": emp})


async def test_user_update_employee_no_reflected(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """employee_no 설정 → 응답/상세에 반영 (선행0 보존)."""
    staff = test_users["teststaff"]["id"]
    try:
        resp = await _set_emp(async_client, admin_headers, staff, "05021")
        assert resp.status_code == 200, resp.text
        assert resp.json()["employee_no"] == "05021"
        detail = await async_client.get(f"{USER_BASE}/{staff}", headers=admin_headers)
        assert detail.json()["employee_no"] == "05021"
    finally:
        await _set_emp(async_client, admin_headers, staff, None)


async def test_user_employee_no_invalid_returns_422(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """공백/특수문자 → 422."""
    staff = test_users["teststaff"]["id"]
    resp = await _set_emp(async_client, admin_headers, staff, "has space!")
    assert resp.status_code == 422, resp.text


async def test_user_duplicate_employee_no_returns_409(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """같은 org 내 동일 사번 → 409."""
    staff = test_users["teststaff"]["id"]
    sv = test_users["testsv"]["id"]
    try:
        r1 = await _set_emp(async_client, admin_headers, staff, "E200")
        assert r1.status_code == 200, r1.text
        r2 = await _set_emp(async_client, admin_headers, sv, "E200")
        assert r2.status_code == 409, r2.text
    finally:
        await _set_emp(async_client, admin_headers, staff, None)
        await _set_emp(async_client, admin_headers, sv, None)


# ── owner gate (service level) ──────────────────────────────


async def test_change_existing_employee_no_requires_owner(
    test_users: dict, seed_organization: dict
):
    """이미 부여된 사번의 '변경'은 Owner 만 — GM 은 ForbiddenError, Owner 는 성공.
    (신규 부여는 users:update 로 가능, 변경만 owner gate.)
    """
    org_id: UUID = seed_organization["id"]
    staff_id = test_users["teststaff"]["id"]
    async with async_session() as db:
        owner = await user_repository.get_detail(db, test_users["testadmin"]["id"], org_id)
        gm = await user_repository.get_detail(db, test_users["testgm"]["id"], org_id)

        # 1) owner 가 최초 부여 (None → E001).
        await user_service.update_user(
            db, staff_id, org_id, UserUpdate(employee_no="E001"), caller=owner
        )

        # 2) GM 이 기존 사번 변경 시도 → Forbidden.
        with pytest.raises(ForbiddenError):
            await user_service.update_user(
                db, staff_id, org_id, UserUpdate(employee_no="E002"), caller=gm
            )

        # 3) owner 는 변경 가능.
        res = await user_service.update_user(
            db, staff_id, org_id, UserUpdate(employee_no="E002"), caller=owner
        )
        assert res.employee_no == "E002"

        # cleanup: owner 가 해제.
        await user_service.update_user(
            db, staff_id, org_id, UserUpdate(employee_no=None), caller=owner
        )
