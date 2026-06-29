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
from sqlalchemy import delete, select, update

from app.database import async_session
from app.models.employee_no_history import EmployeeNoHistory
from app.models.user import User
from app.repositories.user_repository import user_repository
from app.schemas.user import UserUpdate
from app.services import empid_reconcile_service as empid_svc
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


# ── employee_no 이력기반 영구 burn (옵션 A) ─────────────────────


async def _ledger_rows(db, org_id, employee_no):
    """org 에서 해당 사번의 ledger 행 목록."""
    res = await db.execute(
        select(EmployeeNoHistory).where(
            EmployeeNoHistory.organization_id == org_id,
            EmployeeNoHistory.employee_no == employee_no,
        )
    )
    return list(res.scalars().all())


async def test_fresh_assignment_records_ledger_row(
    async_client: AsyncClient, admin_headers: dict, test_users: dict, seed_organization: dict
):
    """신규 부여 성공 → ledger 에 행 적재(first_assigned_user_id=대상)."""
    org_id: UUID = seed_organization["id"]
    staff = test_users["teststaff"]["id"]
    resp = await _set_emp(async_client, admin_headers, staff, "FRESH1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["employee_no"] == "FRESH1"
    async with async_session() as db:
        rows = await _ledger_rows(db, org_id, "FRESH1")
    assert len(rows) == 1
    assert rows[0].first_assigned_user_id == UUID(str(staff))


async def test_previously_used_number_blocked_for_another_user(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """A 가 쓰던 번호를 다른 값으로 바꾼 뒤, 그 옛 번호를 B 에게 부여 → 409 (영구 burn)."""
    staff = test_users["teststaff"]["id"]
    sv = test_users["testsv"]["id"]
    # A 에 BURN1 부여 → 다른 값으로 변경 (admin=super_owner 라 변경 가능)
    r1 = await _set_emp(async_client, admin_headers, staff, "BURN1")
    assert r1.status_code == 200, r1.text
    r2 = await _set_emp(async_client, admin_headers, staff, "BURN2")
    assert r2.status_code == 200, r2.text
    # B 에 옛 번호 BURN1 부여 시도 → 409
    r3 = await _set_emp(async_client, admin_headers, sv, "BURN1")
    assert r3.status_code == 409, r3.text
    assert "previously used" in r3.json()["detail"]


async def test_self_reclaim_blocked_option_a(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """옵션 A: 본인 회수도 불가. A 번호 변경 후 옛 번호 되돌리기 → 409."""
    staff = test_users["teststaff"]["id"]
    r1 = await _set_emp(async_client, admin_headers, staff, "BURN3")
    assert r1.status_code == 200, r1.text
    r2 = await _set_emp(async_client, admin_headers, staff, "BURN4")
    assert r2.status_code == 200, r2.text
    # 옛 번호 BURN3 으로 되돌리기 → 409
    r3 = await _set_emp(async_client, admin_headers, staff, "BURN3")
    assert r3.status_code == 409, r3.text
    assert "previously used" in r3.json()["detail"]


async def test_clear_to_null_keeps_number_burned(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """null 해제는 허용 + 옛 번호는 계속 burn(다른 사람 재부여 → 409)."""
    staff = test_users["teststaff"]["id"]
    sv = test_users["testsv"]["id"]
    r1 = await _set_emp(async_client, admin_headers, staff, "CLR1")
    assert r1.status_code == 200, r1.text
    # null 해제 (admin=owner 라 기존 번호 해제 가능)
    r2 = await _set_emp(async_client, admin_headers, staff, None)
    assert r2.status_code == 200, r2.text
    assert r2.json()["employee_no"] is None
    # 다른 사람에게 CLR1 재부여 → 여전히 409
    r3 = await _set_emp(async_client, admin_headers, sv, "CLR1")
    assert r3.status_code == 409, r3.text


async def test_unchanged_employee_no_no_duplicate_ledger(
    async_client: AsyncClient, admin_headers: dict, test_users: dict, seed_organization: dict
):
    """같은 값 재전송은 no-op — ledger 중복 적재 없음."""
    org_id: UUID = seed_organization["id"]
    staff = test_users["teststaff"]["id"]
    r1 = await _set_emp(async_client, admin_headers, staff, "SAME1")
    assert r1.status_code == 200, r1.text
    # 같은 값 다시 (full_name 등 다른 필드 변경과 함께 보내도 emp 는 unchanged)
    r2 = await async_client.put(
        f"{USER_BASE}/{staff}", headers=admin_headers,
        json={"employee_no": "SAME1", "full_name": "Test Staff"},
    )
    assert r2.status_code == 200, r2.text
    async with async_session() as db:
        rows = await _ledger_rows(db, org_id, "SAME1")
    assert len(rows) == 1  # 중복 적재되지 않음


async def test_leading_zero_preserved_in_ledger(
    async_client: AsyncClient, admin_headers: dict, test_users: dict, seed_organization: dict
):
    """선행0 보존 — '05021' 이 정수화되지 않고 ledger 에도 그대로 적재."""
    org_id: UUID = seed_organization["id"]
    staff = test_users["teststaff"]["id"]
    resp = await _set_emp(async_client, admin_headers, staff, "05021")
    assert resp.status_code == 200, resp.text
    assert resp.json()["employee_no"] == "05021"
    async with async_session() as db:
        rows = await _ledger_rows(db, org_id, "05021")
    assert len(rows) == 1
    assert rows[0].employee_no == "05021"


# ── 백오피스 EMPID 임포트 commit 경로 정합 ─────────────────────


async def test_import_commit_records_ledger_and_blocks_reuse(
    db, seed_organization: dict, test_users: dict
):
    """commit 경로: 성공 부여 → ledger 적재. 과거 사용 번호 → reject(덮어쓰기 없음, no crash)."""
    org_id: UUID = seed_organization["id"]
    u1 = test_users["teststaff"]["id"]
    u2 = test_users["testsv"]["id"]
    # 격리 — 시드유저 사번 + org ledger 비우기 (서비스레벨 테스트라 _clean_state 미적용)
    await db.execute(update(User).where(User.id.in_([u1, u2])).values(employee_no=None))
    await db.execute(delete(EmployeeNoHistory).where(EmployeeNoHistory.organization_id == org_id))
    await db.commit()
    try:
        # 1) NULL → 배정 + ledger 적재
        r = await empid_svc.commit_assignments(db, org_id, [(u1, "IMP-1")])
        assert len(r.assigned) == 1 and not r.rejected, r
        rows = await _ledger_rows(db, org_id, "IMP-1")
        assert len(rows) == 1 and rows[0].first_assigned_user_id == UUID(str(u1))

        # 2) u1 사번 해제(번호가 풀린 것처럼) — ledger 는 그대로 burn 유지
        await db.execute(update(User).where(User.id == u1).values(employee_no=None))
        await db.commit()

        # 3) 과거 사용 번호를 u2 에 → reject (덮어쓰지 않음, crash 없음)
        r = await empid_svc.commit_assignments(db, org_id, [(u2, "IMP-1")])
        assert not r.assigned and len(r.rejected) == 1, r
        assert "already used" in r.rejected[0][1]
        u2_obj = await user_repository.get_by_id(db, u2, org_id)
        assert u2_obj.employee_no is None  # 덮어쓰기 없음
    finally:
        await db.execute(update(User).where(User.id.in_([u1, u2])).values(employee_no=None))
        await db.execute(delete(EmployeeNoHistory).where(EmployeeNoHistory.organization_id == org_id))
        await db.commit()
