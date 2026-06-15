"""Tips 4070 vector signature — unit + API integration tests (merge gate).

통일 벡터 서명(users.signature_strokes) 으로 이행한 4070 서명 흐름 커버:
    - 폼 서명 시 벡터 strokes 가 form.signature_strokes 에 박제된다.
    - PDF 렌더가 strokes 를 image 보다 우선한다 (build_form_4070_pdf).
    - 레거시 signature_image_key 만 있는 구 폼/요청은 여전히 이미지로 렌더된다.
    - save_for_future=True 면 users.signature_strokes 가 갱신된다 (경고와 공용).
    - /signature GET 은 벡터를, /saved-signature PUT 은 벡터를 설정한다.
    - 빈 입력(strokes/image_key 둘 다 없음) 은 422.

전제: startup lifespan 이 테스트에서 안 돌므로 tips:* 권한을 fixture 에서
idempotent 하게 staff role 에 보장한다 (warning/evaluation 테스트 패턴).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.permission import Permission, RolePermission
from app.models.tip import Form4070Document, TipPeriod
from app.models.user import User
from app.utils.form_4070_pdf import build_form_4070_pdf

APP_BASE = "/api/v1/app/my/tips"
TIP_CODES = ["tips:read", "tips:edit_own", "tips:form_view"]

# async API 테스트만 명시적으로 마킹 (sync PDF 단위 테스트는 마킹하지 않음).
_async = pytest.mark.asyncio


def _strokes(*, n: int = 1) -> list[list[list[float]]]:
    """정규화(0..1) 벡터 스트로크 — 테스트용 단순 서명."""
    return [[[0.1 * i, 0.2 * i] for i in range(1, 4)] for _ in range(n)]


def _tiny_png() -> bytes:
    """1x1 검정 PNG — 레거시 이미지 fallback 검증용."""
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8B"
        "QDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


# ===================================================================
# Fixtures
# ===================================================================


async def _login(username: str) -> str:
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


@pytest_asyncio.fixture
async def tip_perms(seed_roles: dict[str, UUID]) -> None:
    """tips:read/edit_own/form_view 를 staff role 에 idempotent 부여."""
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in TIP_CODES:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id

        role_id = seed_roles["staff"]
        for code in TIP_CODES:
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
async def normalize_staff_role(test_users: dict, seed_roles: dict[str, UUID]):
    """teststaff 가 'staff' role 을 가리키도록 보장 (권한 부여 일관성)."""
    staff_role_id = seed_roles["staff"]
    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        if u.role_id != staff_role_id:
            u.role_id = staff_role_id
            await db.commit()


@pytest_asyncio.fixture
async def reset_saved_signature(test_users: dict):
    """teststaff 의 저장 서명(벡터+이미지)을 테스트 전후로 비운다."""
    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        u.signature_strokes = None
        u.signature_image_key = None
        await db.commit()
    yield
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        u.signature_strokes = None
        u.signature_image_key = None
        await db.commit()


@pytest_asyncio.fixture
async def make_form(test_users: dict, test_store_id: UUID):
    """generated 상태 4070 폼 + period 를 직접 생성하는 팩토리 (테스트 setup).

    생성된 (form_id, period_id) 와 정리용 추적을 반환. teardown 에서 삭제.
    """
    created_forms: list[UUID] = []
    created_periods: list[UUID] = []
    staff_uid: UUID = test_users["teststaff"]["id"]

    async def _factory() -> UUID:
        async with async_session() as db:
            period = TipPeriod(
                store_id=test_store_id,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 15),
                status="confirmed",
                confirmed_at=datetime.now(timezone.utc),
            )
            db.add(period)
            await db.flush()
            form = Form4070Document(
                employee_id=staff_uid,
                period_id=period.id,
                reported_cash=Decimal("10.00"),
                reported_card=Decimal("20.00"),
                paid_out=Decimal("0.00"),
                net_tips=Decimal("30.00"),
                status="generated",
            )
            db.add(form)
            await db.flush()
            created_forms.append(form.id)
            created_periods.append(period.id)
            await db.commit()
            return form.id

    yield _factory

    async with async_session() as db:
        if created_forms:
            await db.execute(
                delete(Form4070Document).where(Form4070Document.id.in_(created_forms))
            )
        if created_periods:
            await db.execute(
                delete(TipPeriod).where(TipPeriod.id.in_(created_periods))
            )
        await db.commit()


# ===================================================================
# Unit — PDF render chooses vector over legacy image
# ===================================================================


def test_pdf_render_vector_strokes_smoke():
    """벡터 strokes 만으로 4070 PDF 가 생성된다 (valid PDF)."""
    pdf = build_form_4070_pdf(
        employee_name="Alice",
        employee_email=None,
        period_start="2026-06-01",
        period_end="2026-06-15",
        store_name="Store",
        cash_tips="10.00",
        card_tips="20.00",
        paid_out="0.00",
        net_tips="30.00",
        signed_at="2026-06-12T00:00:00",
        signature_strokes={"strokes": _strokes(n=2), "aspect": 2.5},
    )
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000


def test_pdf_render_legacy_image_fallback_smoke():
    """strokes 없이 레거시 PNG 만으로도 4070 PDF 가 생성된다 (구 폼 호환)."""
    pdf = build_form_4070_pdf(
        employee_name="Bob",
        employee_email=None,
        period_start="2026-06-01",
        period_end="2026-06-15",
        store_name="Store",
        cash_tips="1.00",
        card_tips="2.00",
        paid_out="0.00",
        net_tips="3.00",
        signed_at="2026-06-12T00:00:00",
        signature_png=_tiny_png(),
    )
    assert pdf[:4] == b"%PDF"


def test_pdf_render_strokes_win_over_image(monkeypatch):
    """strokes + image 둘 다 주면 strokes 를 그리고 image 는 무시한다."""
    import app.utils.form_4070_pdf as mod

    called = {"image": False, "strokes": False}
    real_draw = mod._draw_signature_strokes

    def spy_draw(pdf, signature_strokes, box_top):
        called["strokes"] = True
        return real_draw(pdf, signature_strokes, box_top)

    monkeypatch.setattr(mod, "_draw_signature_strokes", spy_draw)

    # FPDF.image 가 호출되면 fail 하도록 — strokes 가 image 를 막아야 함.
    from fpdf import FPDF

    orig_image = FPDF.image

    def guard_image(self, *a, **k):
        called["image"] = True
        return orig_image(self, *a, **k)

    monkeypatch.setattr(FPDF, "image", guard_image)

    mod.build_form_4070_pdf(
        employee_name="X",
        employee_email=None,
        period_start="a",
        period_end="b",
        store_name="S",
        cash_tips="1.00",
        card_tips="2.00",
        paid_out="0.00",
        net_tips="3.00",
        signed_at="t",
        signature_png=_tiny_png(),
        signature_strokes={"strokes": _strokes(), "aspect": 2.0},
    )
    assert called["strokes"] is True
    assert called["image"] is False, "legacy image must not be drawn when strokes exist"


# ===================================================================
# API — sign stores vector strokes
# ===================================================================


@_async
async def test_sign_form_stores_vector_strokes(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature, make_form,
):
    form_id = await make_form()
    token = await _login("teststaff")

    resp = await async_client.post(
        f"{APP_BASE}/forms/{form_id}/sign",
        headers=_hdr(token),
        json={"strokes": _strokes(n=2), "aspect": 2.0, "method": "drawn"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "signed"
    assert body["signature_strokes"] is not None
    assert body["signature_strokes"]["strokes"]
    assert body["signature_image_key"] is None

    # DB 박제 확인
    async with async_session() as db:
        form = (
            await db.execute(
                select(Form4070Document).where(Form4070Document.id == form_id)
            )
        ).scalar_one()
        assert form.signature_strokes is not None
        assert form.signature_image_key is None
        assert form.status == "signed"


@_async
async def test_sign_form_save_for_future_updates_user_vector(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature,
    make_form, test_users,
):
    form_id = await make_form()
    token = await _login("teststaff")

    resp = await async_client.post(
        f"{APP_BASE}/forms/{form_id}/sign",
        headers=_hdr(token),
        json={
            "strokes": _strokes(),
            "aspect": 1.5,
            "method": "drawn",
            "save_for_future": True,
        },
    )
    assert resp.status_code == 200, resp.text

    # users.signature_strokes 가 갱신되었는지 — 경고와 공용 통일 서명.
    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        assert u.signature_strokes is not None
        assert u.signature_strokes["aspect"] == 1.5


@_async
async def test_sign_form_legacy_image_key_still_works(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature, make_form,
):
    """strokes 없이 signature_image_key 만 보내는 구 클라이언트 — 여전히 서명된다."""
    form_id = await make_form()
    token = await _login("teststaff")

    resp = await async_client.post(
        f"{APP_BASE}/forms/{form_id}/sign",
        headers=_hdr(token),
        json={"signature_image_key": "signatures/users/legacy/x.png"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "signed"
    assert body["signature_image_key"] == "signatures/users/legacy/x.png"
    assert body["signature_strokes"] is None


@_async
async def test_sign_form_rejects_empty_signature(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature, make_form,
):
    form_id = await make_form()
    token = await _login("teststaff")
    resp = await async_client.post(
        f"{APP_BASE}/forms/{form_id}/sign",
        headers=_hdr(token),
        json={"method": "drawn"},
    )
    assert resp.status_code == 422, resp.text


@_async
async def test_sign_form_already_signed_rejected(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature, make_form,
):
    form_id = await make_form()
    token = await _login("teststaff")
    first = await async_client.post(
        f"{APP_BASE}/forms/{form_id}/sign",
        headers=_hdr(token),
        json={"strokes": _strokes(), "aspect": 2.0},
    )
    assert first.status_code == 200, first.text
    second = await async_client.post(
        f"{APP_BASE}/forms/{form_id}/sign",
        headers=_hdr(token),
        json={"strokes": _strokes(), "aspect": 2.0},
    )
    assert second.status_code == 400, second.text


# ===================================================================
# API — saved signature unification (users.signature_strokes)
# ===================================================================


@_async
async def test_saved_signature_put_then_get_vector(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature,
):
    token = await _login("teststaff")

    put = await async_client.put(
        f"{APP_BASE}/saved-signature",
        headers=_hdr(token),
        json={"strokes": _strokes(n=2), "aspect": 3.0},
    )
    assert put.status_code == 200, put.text
    assert put.json()["signature_strokes"]["aspect"] == 3.0

    get = await async_client.get(f"{APP_BASE}/signature", headers=_hdr(token))
    assert get.status_code == 200, get.text
    body = get.json()
    assert body["signature_strokes"] is not None
    assert body["signature_strokes"]["aspect"] == 3.0


@_async
async def test_saved_signature_put_rejects_unnormalized(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature,
):
    token = await _login("teststaff")
    resp = await async_client.put(
        f"{APP_BASE}/saved-signature",
        headers=_hdr(token),
        json={"strokes": [[[2.0, 0.5], [0.1, 0.1]]], "aspect": 1.0},
    )
    assert resp.status_code == 422, resp.text


@_async
async def test_delete_signature_clears_vector(
    async_client, tip_perms, normalize_staff_role, reset_saved_signature, test_users,
):
    token = await _login("teststaff")
    await async_client.put(
        f"{APP_BASE}/saved-signature",
        headers=_hdr(token),
        json={"strokes": _strokes(), "aspect": 2.0},
    )
    resp = await async_client.delete(f"{APP_BASE}/signature", headers=_hdr(token))
    assert resp.status_code == 204, resp.text

    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        assert u.signature_strokes is None
        assert u.signature_image_key is None
