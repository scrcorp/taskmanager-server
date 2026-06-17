"""Phase 3 — wet 서명(출력→실물 서명→PDF 업로드) + 방식 전환 테스트.

대상:
    - POST /console/warnings (signature_method=wet)
    - POST /console/warnings/{id}/signed-pdf  (업로드 = 서명완료, 권한/검증)
    - GET  /console/warnings/{id}/signed-pdf  (다운로드 + Content-Disposition, IDOR)
    - PUT  /console/warnings/{id}/method      (전환 → 무효화/재서명)
    - app  /my/warnings/unsigned-count        (wet 제외)
    - upload_wet_pdf 권한 게이트 (service)
    - build_warning_filename 포맷
"""
from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.models.user_store import UserStore
from app.models.warning import Warning
from app.repositories.user_repository import user_repository
from app.services.warning_service import warning_service
from app.utils.exceptions import ForbiddenError

BASE = "/api/v1/console/warnings"
APP_BASE = "/api/v1/app/my/warnings"

WARNING_CODES = ["warnings:read", "warnings:create", "warnings:update", "warnings:delete"]


# ── 공용 fixtures (test_warnings.py 와 동형 — 자체 포함) ──────


@pytest_asyncio.fixture
async def warning_perms(seed_roles: dict) -> None:
    """warnings:* (CRUD) 를 general_manager role 에 idempotent 부여."""
    async with async_session() as db:
        perms: dict = {}
        for code in WARNING_CODES:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id
        role_id = seed_roles["general_manager"]
        for code in WARNING_CODES:
            exists = (
                await db.execute(
                    select(RolePermission).where(
                        RolePermission.role_id == role_id,
                        RolePermission.permission_id == perms[code],
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                db.add(RolePermission(role_id=role_id, permission_id=perms[code]))
        await db.commit()


@pytest_asyncio.fixture
async def normalize_staff_role(test_users: dict, seed_roles: dict):
    staff_role_id = seed_roles["staff"]
    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        if u.role_id != staff_role_id:
            u.role_id = staff_role_id
            await db.commit()


@pytest_asyncio.fixture
async def assign_stores(test_users: dict, test_store_id: UUID, normalize_staff_role):
    async with async_session() as db:
        for uname, is_manager in (("testgm", True), ("testsv", False), ("teststaff", False)):
            uid = test_users[uname]["id"]
            us = (
                await db.execute(
                    select(UserStore).where(
                        UserStore.user_id == uid, UserStore.store_id == test_store_id
                    )
                )
            ).scalar_one_or_none()
            if us is None:
                db.add(UserStore(user_id=uid, store_id=test_store_id, is_manager=is_manager))
            else:
                us.is_manager = is_manager
        await db.commit()
    yield
    async with async_session() as db:
        for uname in ("testgm", "testsv", "teststaff"):
            uid = test_users[uname]["id"]
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == test_store_id
                )
            )
        await db.commit()


@pytest_asyncio.fixture
async def cleanup_warnings(seed_organization: dict):
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        await db.execute(delete(Warning).where(Warning.organization_id == org_id))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(Warning).where(Warning.organization_id == org_id))
        await db.commit()

# 최소 유효 PDF (매직바이트 %PDF-).
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
_NOT_PDF = b"GIF89a not a pdf at all"


async def _login(username: str) -> str:
    """username → access token (직접 mint — staff 포함 app+console 공용)."""
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(subject, store_id, *, method="digital", categories=None):
    return {
        "subject_user_id": str(subject),
        "store_id": str(store_id),
        "title": "Wet sign test",
        "categories": categories or ["tardiness"],
        "details": "x",
        "warning_date": "2026-06-01",
        "signature_method": method,
    }


async def _create(client, token, subject, store_id, **kw):
    r = await client.post(f"{BASE}/", json=_payload(subject, store_id, **kw), headers=_hdr(token))
    assert r.status_code == 201, r.text
    return r.json()


# ── method on create ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_with_wet_method(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    body = await _create(async_client, token, subject, test_store_id, method="wet")
    assert body["signature_method"] == "wet"
    # 미업로드 → 서명완료 아님.
    assert body["signed_pdf_present"] is False
    assert body["employee_signed"] is False


# ── upload ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_signed_pdf_completes(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="wet")
    r = await async_client.post(
        f"{BASE}/{w['id']}/signed-pdf",
        headers=_hdr(token),
        files={"file": ("scan.pdf", _PDF, "application/pdf")},
        data={"signed_on": "2026-06-12"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # wet PDF 가 직원+매니저 양쪽 갈음.
    assert body["signed_pdf_present"] is True
    assert body["employee_signed"] is True
    assert body["manager_signed"] is True
    assert body["wet_signed_on"] == "2026-06-12"


@pytest.mark.asyncio
async def test_upload_requires_wet_method(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """digital 경고에 업로드 → 400 (먼저 wet 전환 필요)."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="digital")
    r = await async_client.post(
        f"{BASE}/{w['id']}/signed-pdf",
        headers=_hdr(token),
        files={"file": ("scan.pdf", _PDF, "application/pdf")},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_upload_rejects_non_pdf(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="wet")
    # content_type 은 pdf 라 속여도 매직바이트에서 걸림.
    r = await async_client.post(
        f"{BASE}/{w['id']}/signed-pdf",
        headers=_hdr(token),
        files={"file": ("fake.pdf", _NOT_PDF, "application/pdf")},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_owner_can_upload_others_warning(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """testgm 이 발행한 wet 경고를 owner(testadmin)가 업로드 → 200 (대리 업로드 허용)."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, gm, subject, test_store_id, method="wet")
    admin = await _login("testadmin")
    r = await async_client.post(
        f"{BASE}/{w['id']}/signed-pdf",
        headers=_hdr(admin),
        files={"file": ("scan.pdf", _PDF, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["signed_pdf_present"] is True


# ── switch (invalidation) ───────────────────────────────────


@pytest.mark.asyncio
async def test_switch_digital_to_wet_invalidates_signature(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """digital 서명 후 wet 전환 → 기존 매니저 서명 무효화."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="digital")
    # 매니저(발행자 본인) 디지털 서명.
    sign = await async_client.post(
        f"{BASE}/{w['id']}/sign",
        headers=_hdr(token),
        json={"strokes": [[[0.1, 0.1], [0.9, 0.9]]], "aspect": 2.0, "method": "drawn"},
    )
    assert sign.status_code == 200, sign.text
    assert sign.json()["manager_signed"] is True
    # wet 전환.
    sw = await async_client.put(
        f"{BASE}/{w['id']}/method", headers=_hdr(token), json={"method": "wet"}
    )
    assert sw.status_code == 200, sw.text
    assert sw.json()["signature_method"] == "wet"
    assert sw.json()["manager_signed"] is False  # 무효화됨
    assert sw.json()["signatures"]["manager"] is None


@pytest.mark.asyncio
async def test_switch_wet_to_digital_clears_pdf(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """wet 업로드 후 digital 전환 → PDF 무효화 (signed_pdf_present false)."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="wet")
    up = await async_client.post(
        f"{BASE}/{w['id']}/signed-pdf",
        headers=_hdr(token),
        files={"file": ("scan.pdf", _PDF, "application/pdf")},
    )
    assert up.json()["signed_pdf_present"] is True
    sw = await async_client.put(
        f"{BASE}/{w['id']}/method", headers=_hdr(token), json={"method": "digital"}
    )
    assert sw.status_code == 200, sw.text
    assert sw.json()["signature_method"] == "digital"
    assert sw.json()["signed_pdf_present"] is False
    assert sw.json()["employee_signed"] is False


# ── download ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_signed_pdf(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="wet")
    await async_client.post(
        f"{BASE}/{w['id']}/signed-pdf",
        headers=_hdr(token),
        files={"file": ("scan.pdf", _PDF, "application/pdf")},
        data={"signed_on": "2026-06-12"},
    )
    r = await async_client.get(f"{BASE}/{w['id']}/signed-pdf", headers=_hdr(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and cd.endswith('.pdf"')
    assert "2026-06-12" in cd  # 서명일 기반 파일명 (YYYY-MM-DD)
    assert r.content.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_download_missing_pdf_404(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="wet")
    r = await async_client.get(f"{BASE}/{w['id']}/signed-pdf", headers=_hdr(token))
    assert r.status_code == 404, r.text


# ── app: wet 제외 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_wet_excluded_from_unsigned_count(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """wet 경고는 직원이 앱서 서명 불가 → unsigned-count 에서 제외."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    await _create(async_client, gm, subject, test_store_id, method="digital")  # 카운트 1
    await _create(async_client, gm, subject, test_store_id, method="wet")  # 제외
    staff = await _login("teststaff")
    r = await async_client.get(f"{APP_BASE}/unsigned-count", headers=_hdr(staff))
    assert r.status_code == 200, r.text
    assert r.json()["unsigned_count"] == 1  # wet 제외, digital 1 건만


# ── service: 권한 게이트 ────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_gate_non_issuer_without_permission_forbidden(
    warning_perms, assign_stores, cleanup_warnings, test_users, seed_organization, test_store_id
):
    """발행자 아님 + can_upload_others=False → ForbiddenError (service)."""
    org_id = seed_organization["id"]
    async with async_session() as db:
        # testsv 를 비-발행자 업로더로 사용 (issued_by_id 는 testgm).
        from app.schemas.warning import WarningCreate

        gm = await user_repository.get_detail(db, test_users["testgm"]["id"], org_id)
        sv = await user_repository.get_detail(db, test_users["testsv"]["id"], org_id)
        w = await warning_service.create_warning(
            db,
            organization_id=org_id,
            issuer=gm,
            data=WarningCreate(**_payload(test_users["teststaff"]["id"], test_store_id, method="wet")),
        )

        async def _noop(_):
            return None

        with pytest.raises(ForbiddenError):
            await warning_service.upload_wet_pdf(
                db,
                warning_id=w.id,
                organization_id=org_id,
                uploader=sv,
                can_upload_others=False,
                pdf_bytes=_PDF,
                filename="x.pdf",
                signed_on=None,
                check_store_access=_noop,
            )
