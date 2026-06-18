"""경고 문서 PDF — 서버 렌더(WeasyPrint) 엔드포인트 + 렌더 서비스 테스트.

대상:
    - GET /console/warnings/{id}/pdf  (digital/wet 모두 문서 생성, 권한, not-found)
    - warning_pdf_service.render_document  (긴 내용 → 다중 페이지 보장)

fixtures 는 test_warning_wetsign.py 와 동형(자체 포함).
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.models.user_store import UserStore
from app.models.warning import Warning
from app.services.warning_pdf_service import warning_pdf_service

BASE = "/api/v1/console/warnings"
WARNING_CODES = ["warnings:read", "warnings:create", "warnings:update", "warnings:delete"]


# ── 공용 fixtures ────────────────────────────────────────────


@pytest_asyncio.fixture
async def warning_perms(seed_roles: dict) -> None:
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


async def _login(username: str) -> str:
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token({"sub": str(user.id), "org": str(user.organization_id)})


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(subject, store_id, *, method="digital", details="x", categories=None):
    return {
        "subject_user_id": str(subject),
        "store_id": str(store_id),
        "title": "PDF doc test",
        "categories": categories or ["tardiness"],
        "details": details,
        "warning_date": "2026-06-01",
        "signature_method": method,
    }


async def _create(client, token, subject, store_id, **kw):
    r = await client.post(f"{BASE}/", json=_payload(subject, store_id, **kw), headers=_hdr(token))
    assert r.status_code == 201, r.text
    return r.json()


# ── endpoint: 문서 PDF 다운로드 ──────────────────────────────


@pytest.mark.asyncio
async def test_download_warning_pdf_digital(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="digital")

    r = await async_client.get(f"{BASE}/{w['id']}/pdf", headers=_hdr(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and cd.endswith('.pdf"')
    assert "2026-06-01" in cd  # warning_date 기반 파일명 (YYYY-MM-DD)
    assert r.content.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_download_warning_pdf_wet_without_scan(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """wet 경고는 스캔 업로드 전에도 '출력→서명용' 문서 PDF 가 생성된다."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, token, subject, test_store_id, method="wet")

    r = await async_client.get(f"{BASE}/{w['id']}/pdf", headers=_hdr(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_warning_pdf_forbidden_without_permission(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """warnings:read 없는 staff → 403 (handler 도달 전 권한 게이트)."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    w = await _create(async_client, gm, subject, test_store_id, method="digital")

    staff = await _login("teststaff")
    r = await async_client.get(f"{BASE}/{w['id']}/pdf", headers=_hdr(staff))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_warning_pdf_not_found(
    async_client, warning_perms, assign_stores, cleanup_warnings
):
    token = await _login("testgm")
    r = await async_client.get(f"{BASE}/{uuid4()}/pdf", headers=_hdr(token))
    assert r.status_code == 404, r.text


# ── service: 다중 페이지 보장 (문서 서식의 핵심) ──────────────


def _doc_data(details: str) -> dict:
    return {
        "ref_no": "W-000001",
        "subject_name": "Maria Garcia",
        "employee_no": "E1042",
        "issued_by_name": "James Park",
        "store_name": "Il Fiora",
        "store_code": "IFO",
        "title": "Repeated lateness",
        "categories": ["tardiness"],
        "category_labels": {"tardiness": "Tardiness / Attendance"},
        "details": details,
        "corrective_action": "Arrive on time.",
        "other_text": None,
        "deadline": date(2026, 6, 24),
        "follow_up_date": date(2026, 7, 1),
        "follow_up_time": "14:00",
        "warning_date": date(2026, 6, 17),
        "ordinal": 2,
        "signature_method": "digital",
        "acknowledged_at": datetime(2026, 6, 18, 9, 30),
        "employee_signed": True,
        "manager_signed": True,
    }


def test_render_short_warning_single_page():
    doc = warning_pdf_service.render_document(_doc_data("Brief note."))
    assert len(doc.pages) == 1
    assert doc.write_pdf().startswith(b"%PDF-")


def test_render_reproduces_form_hides_subject_shows_reasons():
    """폼 재현 — banner 타이틀은 나오고 Subject(제목)는 PDF 에서 빠진다.
    사유는 선택 여부와 무관하게 org 전체 옵션이 체크리스트로 나온다."""
    import io

    pypdf = pytest.importorskip("pypdf")
    data = _doc_data("Brief note.")
    data["title"] = "SECRETSUBJECTXYZ"  # 화면 전용 — PDF 에 나오면 안 됨
    cats = [{"code": "tardiness", "label": "Tardiness"}, {"code": "safety", "label": "SafetyMarker"}]
    pdf = warning_pdf_service.render_pdf(data, cats)
    text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    assert "WARNING NOTICE FORM" in text.upper()  # banner 타이틀(폼과 동일)
    assert "SECRETSUBJECTXYZ" not in text  # Subject 숨김
    assert "SafetyMarker" in text  # 선택 안 한 사유도 전체 체크리스트로 표시


def test_signature_svg_helper():
    """벡터 서명 → inline SVG (SignatureView 포팅). 결손/빈 stroke 는 빈 문자열."""
    from app.services.warning_pdf_service import _signature_svg

    svg = _signature_svg({"strokes": [[[0.1, 0.5], [0.4, 0.2], [0.7, 0.6]]], "aspect": 2.6})
    assert svg.startswith("<svg") and "<path" in svg and "M " in svg
    assert _signature_svg(None) == ""
    assert _signature_svg({"strokes": [], "aspect": 2.6}) == ""


def test_render_digital_signed_embeds_signature():
    """digital 서명된 경고도 무오류 렌더(서명 벡터 ink 포함)."""
    data = _doc_data("Brief.")
    data["signatures"] = {
        "employee": {
            "signer_name": "Test Staff",
            "signed_at": datetime(2026, 6, 18, 9, 30),
            "signature_strokes": {"strokes": [[[0.1, 0.5], [0.5, 0.2], [0.9, 0.6]]], "aspect": 2.6},
        },
        "manager": None,
    }
    pdf = warning_pdf_service.render_pdf(data, [{"code": "tardiness", "label": "Tardiness"}])
    assert pdf.startswith(b"%PDF-")


def test_signature_svg_never_crashes_on_bad_data():
    """깨진/형식오류 stroke 에도 예외 없이 빈 문자열 폴백 (prod 500 회귀 방지).
    JS(SignatureView)는 `?? 0` 으로 관대했는데 Python 포팅이 엄격해 터졌던 케이스."""
    from app.services.warning_pdf_service import _signature_svg

    bad = [
        {"strokes": "notalist", "aspect": 2.6},
        {"strokes": [["x", "y"]], "aspect": 2.6},            # 좌표가 숫자 아님
        {"strokes": [[[0.1], [0.2, 0.3]]], "aspect": None},  # 좌표 결손 + aspect None
        {"strokes": [[None, 5]], "aspect": 0},               # None 점 + aspect 0
        {"strokes": [42]},                                   # stroke 가 리스트 아님
        {"aspect": 2.6},                                     # strokes 없음
        None, [], "str", 123,                                # payload 자체가 비정상
    ]
    for b in bad:
        assert isinstance(_signature_svg(b), str)  # 예외 없이 문자열


def test_render_with_malformed_signature_does_not_crash():
    """경고에 깨진 서명 데이터가 있어도 PDF 렌더가 죽지 않는다(prod 500 회귀 방지)."""
    data = _doc_data("Brief.")
    data["signatures"] = {
        "employee": {
            "signer_name": "X",
            "signed_at": datetime(2026, 6, 18, 9, 30),
            "signature_strokes": {"strokes": [["bad", "data"]], "aspect": None},
        },
        "manager": None,
    }
    pdf = warning_pdf_service.render_pdf(data, [{"code": "tardiness", "label": "Tardiness"}])
    assert pdf.startswith(b"%PDF-")


def test_build_warning_filename_handles_none_date():
    """warning_date/wet_signed_on 둘 다 None 이어도 파일명 생성 무오류(날짜=NA)."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services.warning_service import warning_service as ws

    w = SimpleNamespace(
        wet_signed_on=None, warning_date=None, id=uuid4(),
        categories=["tardiness"], ordinal_snapshot=1,
    )
    fn = ws.build_warning_filename(
        w, subject_name="A B", employee_no=None, store_code=None,
        category_labels={"tardiness": "Tardiness"},
    )
    assert fn.endswith(".pdf") and "NA" in fn


def test_render_long_warning_multi_page():
    """긴 details → 여러 페이지로 나뉜다 (grid 인쇄로는 안 되던 핵심)."""
    long_details = "\n\n".join(
        f"{i}. " + ("lorem ipsum dolor sit amet consectetur adipiscing elit. " * 12)
        for i in range(1, 12)
    )
    doc = warning_pdf_service.render_document(_doc_data(long_details))
    assert len(doc.pages) > 1


def test_render_long_warning_no_content_clipped():
    """긴 details/corrective 가 페이지 경계를 넘어도 잘리지 않는다.

    table 셀(박스 폼)이 tall 내용을 clip 하면 데이터 유실 → 회귀 방지.
    처음/마지막 단락 마커가 추출 텍스트에 모두 있어야 한다.
    """
    import io

    pypdf = pytest.importorskip("pypdf")
    s1 = "\n\n".join(f"DETAILMARK{i} " + ("filler " * 60) for i in range(1, 12))
    s2 = "\n\n".join(f"ACTIONMARK{i} " + ("filler " * 60) for i in range(1, 10))
    data = _doc_data(s1)
    data["corrective_action"] = s2

    pdf = warning_pdf_service.render_pdf(data)
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) > 1
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "DETAILMARK1 " in text and "DETAILMARK11 " in text  # 첫·마지막 details 단락
    assert "ACTIONMARK1 " in text and "ACTIONMARK9 " in text  # 첫·마지막 corrective 단락


def test_render_handles_missing_optional_fields():
    """결손 필드(이름/매장/카테고리/일정 None)에도 렌더 무오류."""
    data = {
        "ref_no": None, "subject_name": None, "employee_no": None,
        "issued_by_name": None, "store_name": None, "store_code": None,
        "title": None, "categories": [], "category_labels": {},
        "details": None, "corrective_action": None, "other_text": None,
        "deadline": None, "follow_up_date": None, "follow_up_time": None,
        "warning_date": date(2026, 6, 17), "ordinal": None,
        "signature_method": "wet", "acknowledged_at": None,
        "employee_signed": False, "manager_signed": False,
    }
    assert warning_pdf_service.render_pdf(data).startswith(b"%PDF-")
