"""Console access-codes API — 운영자 지정(PUT) + rotate 감사.

대상:
    - PUT  /api/v1/console/access-codes/{service_key}   (운영자 지정 코드)
    - POST /api/v1/console/access-codes/{service_key}/rotate (감사 기록 추가분)

검증:
    - PUT 성공: source=manual, 소문자 입력이 대문자로 저장
    - 타 org 점유 코드 → 409 detail.code=access_code_taken
    - 짧은 코드 → 422
    - 권한 없는 유저(teststaff) → 403
    - manual_set / rotate 각각 감사 행 생성 (action/actor/meta.code_length,
      meta 에 코드 값 미포함)

주의: 공유 attendance 코드를 바꾸는 테스트는 끝에서 rotate 로 복원한다
(기존 multi_org_isolation 테스트와 동일하게, 코드는 항상 동적으로 읽는 전제).
"""
from __future__ import annotations

import json
import uuid

from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.access_code_audit import AccessCodeAudit
from app.models.organization import Organization

BASE = "/api/v1/console/access-codes"
SVC = "attendance"


async def _latest_audit(org_id: uuid.UUID) -> AccessCodeAudit | None:
    """seed org 의 가장 최근 감사 행 1건."""
    async with async_session() as db:
        result = await db.execute(
            select(AccessCodeAudit)
            .where(
                AccessCodeAudit.organization_id == org_id,
                AccessCodeAudit.service_key == SVC,
            )
            .order_by(AccessCodeAudit.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _rotate_back(client: AsyncClient, headers: dict) -> None:
    """공유 attendance 코드를 랜덤 auto 코드로 복원 (테스트 후 정리)."""
    resp = await client.post(f"{BASE}/{SVC}/rotate", headers=headers)
    assert resp.status_code == 200, resp.text


# ── PUT 성공 ────────────────────────────────────────────────


async def test_put_sets_manual_code_uppercased(
    async_client: AsyncClient, admin_headers: dict, attendance_access_code: str
):
    """소문자 입력 → 대문자로 저장, source=manual, GET 에도 반영."""
    try:
        resp = await async_client.put(
            f"{BASE}/{SVC}", headers=admin_headers, json={"code": "putcode99"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["service_key"] == SVC
        assert body["code"] == "PUTCODE99"
        assert body["source"] == "manual"
        assert body["rotated_at"] is not None

        detail = await async_client.get(f"{BASE}/{SVC}", headers=admin_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["code"] == "PUTCODE99"
        assert detail.json()["source"] == "manual"
    finally:
        await _rotate_back(async_client, admin_headers)


async def test_put_same_code_noop_returns_200(
    async_client: AsyncClient, admin_headers: dict, attendance_access_code: str
):
    """자기 코드 재제출은 no-op — 200, rotated_at 불변."""
    try:
        r1 = await async_client.put(
            f"{BASE}/{SVC}", headers=admin_headers, json={"code": "noopcode1"}
        )
        assert r1.status_code == 200, r1.text
        r2 = await async_client.put(
            f"{BASE}/{SVC}", headers=admin_headers, json={"code": "NOOPCODE1"}
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["code"] == "NOOPCODE1"
        assert r2.json()["rotated_at"] == r1.json()["rotated_at"]
    finally:
        await _rotate_back(async_client, admin_headers)


# ── 타 org 점유 → 409 ───────────────────────────────────────


async def test_put_code_taken_by_other_org_409(
    async_client: AsyncClient, admin_headers: dict, attendance_access_code: str
):
    """다른 조직이 이미 쓰는 코드 → 409 access_code_taken, 자기 코드 불변."""
    from app.core.access_code import set_code

    async with async_session() as db:
        other_org = Organization(name="__AC_TAKEN_ORG__")
        db.add(other_org)
        await db.flush()
        other_org_id = other_org.id
        await set_code(db, SVC, other_org_id, "TAKEN409ZZ")
        await db.commit()

    try:
        before = await async_client.get(f"{BASE}/{SVC}", headers=admin_headers)
        resp = await async_client.put(
            f"{BASE}/{SVC}", headers=admin_headers, json={"code": "taken409zz"}
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "access_code_taken"
        assert detail["message"]

        after = await async_client.get(f"{BASE}/{SVC}", headers=admin_headers)
        assert after.json()["code"] == before.json()["code"]  # 자기 코드 불변
    finally:
        async with async_session() as db:
            # access_codes 는 org FK CASCADE 로 같이 삭제
            await db.execute(
                delete(Organization).where(Organization.id == other_org_id)
            )
            await db.commit()


# ── 422 검증 ────────────────────────────────────────────────


async def test_put_short_code_422(async_client: AsyncClient, admin_headers: dict):
    """4자 미만 → 422 (스키마 validator)."""
    resp = await async_client.put(
        f"{BASE}/{SVC}", headers=admin_headers, json={"code": "abc"}
    )
    assert resp.status_code == 422, resp.text
    assert "Code must be at least 4 characters." in resp.text


async def test_put_internal_space_422(async_client: AsyncClient, admin_headers: dict):
    """내부 공백 → 422."""
    resp = await async_client.put(
        f"{BASE}/{SVC}", headers=admin_headers, json={"code": "AB CD"}
    )
    assert resp.status_code == 422, resp.text
    assert "Code cannot contain spaces." in resp.text


# ── 권한 ────────────────────────────────────────────────────


async def test_put_forbidden_for_staff_403(
    async_client: AsyncClient, test_users: dict
):
    """attendance_devices:update 없는 staff → 403."""
    from app.utils.jwt import create_access_token

    staff = test_users["teststaff"]
    token = create_access_token(
        {"sub": str(staff["id"]), "org": str(staff["organization_id"])}
    )
    resp = await async_client.put(
        f"{BASE}/{SVC}",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": "STAFFTRY1"},
    )
    assert resp.status_code == 403, resp.text


# ── 감사 기록 ───────────────────────────────────────────────


async def test_manual_set_writes_audit_row(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    attendance_access_code: str,
):
    """PUT → manual_set 감사 행: action/actor/meta.code_length, 코드 값 미저장."""
    org_id = test_users["testadmin"]["organization_id"]
    try:
        resp = await async_client.put(
            f"{BASE}/{SVC}", headers=admin_headers, json={"code": "auditcode1"}
        )
        assert resp.status_code == 200, resp.text

        row = await _latest_audit(org_id)
        assert row is not None
        assert row.action == "manual_set"
        assert row.actor_user_id == test_users["testadmin"]["id"]
        assert row.meta == {"code_length": len("AUDITCODE1")}
        # 코드 값은 meta 어디에도 없어야 함 (자격증명 사본 금지)
        assert "AUDITCODE1" not in json.dumps(row.meta)
    finally:
        await _rotate_back(async_client, admin_headers)


async def test_rotate_writes_audit_row(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    attendance_access_code: str,
):
    """rotate → rotate 감사 행: action/actor/meta.code_length, 코드 값 미저장."""
    org_id = test_users["testadmin"]["organization_id"]
    resp = await async_client.post(f"{BASE}/{SVC}/rotate", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    new_code = resp.json()["code"]

    row = await _latest_audit(org_id)
    assert row is not None
    assert row.action == "rotate"
    assert row.actor_user_id == test_users["testadmin"]["id"]
    assert row.meta == {"code_length": len(new_code)}
    assert new_code not in json.dumps(row.meta)


async def test_get_autocreate_writes_no_audit(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """GET 의 ensure(자동 생성) 경로는 감사를 남기지 않는다 (시스템 행위).

    임의 신규 service_key 로 GET → 코드 자동 생성되지만 감사 행 0건.
    """
    org_id = test_users["testadmin"]["organization_id"]
    svc = "__ac_get_test__"
    try:
        resp = await async_client.get(f"{BASE}/{svc}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "auto"

        async with async_session() as db:
            result = await db.execute(
                select(AccessCodeAudit).where(
                    AccessCodeAudit.organization_id == org_id,
                    AccessCodeAudit.service_key == svc,
                )
            )
            assert result.scalars().first() is None
    finally:
        async with async_session() as db:
            from app.models.access_code import AccessCode

            await db.execute(
                delete(AccessCode).where(AccessCode.service_key == svc)
            )
            await db.commit()
