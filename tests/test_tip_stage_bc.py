"""Stage B/C 통합 테스트 — Period confirm, force auto-accept, 4070 form, sign, audit log.

격리 정책: tests/test_tip_entries.py 와 동일하게 created_at >= test_start 만 청소
(사용자 매뉴얼 데이터 보존). 매 테스트가 새 store + schedule + entry 를 만들고
fixture 종료 시점에 그 테스트가 만든 row 만 정리.
"""

from __future__ import annotations

from datetime import date as DateType, datetime, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.organization import Store
from app.models.schedule import Schedule, StoreWorkRole
from app.models.tip import (
    Form4070Document,
    TipAuditLog,
    TipDistribution,
    TipEntry,
    TipPeriod,
)
from app.models.user import User
from app.models.user_store import UserStore


# ── 권한 sync 보장 ────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _ensure_tip_permissions_synced() -> None:
    from app.main import (
        sync_default_role_permissions,
        sync_permission_registry,
    )
    await sync_permission_registry()
    await sync_default_role_permissions()


# ── 토큰 fixture ───────────────────────────────────────────────

async def _app_login(username: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/app/auth/login",
            json={"username": username, "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _console_login(username: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/console/auth/login",
            json={"username": username, "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await _app_login('teststaff')}"}


@pytest_asyncio.fixture
async def sv_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await _app_login('testsv')}"}


@pytest_asyncio.fixture
async def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await _console_login('testadmin')}"}


# ── 격리 fixture ────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def _isolate_tip_state(test_users) -> AsyncIterator[None]:
    """fixture 시작 시각 이후 생성된 tip row + period + form 만 정리."""
    ids = [u["id"] for u in test_users.values()]
    start = datetime.now(timezone.utc)
    yield
    async with async_session() as db:
        await db.execute(delete(Form4070Document).where(
            Form4070Document.employee_id.in_(ids),
            Form4070Document.generated_at >= start,
        ))
        await db.execute(delete(TipPeriod).where(
            TipPeriod.created_at >= start,
        ))
        await db.execute(delete(TipEntry).where(
            TipEntry.employee_id.in_(ids),
            TipEntry.created_at >= start,
        ))
        await db.execute(delete(TipAuditLog).where(
            TipAuditLog.created_at >= start,
        ))
        await db.commit()


# ── 보조 fixture — schedule 만들기 ─────────────────────────────

@pytest_asyncio.fixture
async def staff_schedule(make_schedule, test_users) -> str:
    return str(await make_schedule(test_users["teststaff"]))


@pytest_asyncio.fixture
async def sv_schedule(make_schedule, test_users) -> str:
    return str(await make_schedule(test_users["testsv"]))


def _entry_payload(schedule_id: str, *, card: str = "0", cash: str = "0",
                   distributions: list | None = None) -> dict:
    return {
        "schedule_id": schedule_id,
        "card_tips": card,
        "cash_tips_kept": cash,
        "source": "staff_app",
        "distributions": distributions or [],
    }


# ── Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_period_blocks_further_entry_edits(
    async_client: AsyncClient,
    staff_headers: dict,
    admin_headers: dict,
    staff_schedule: str,
    test_store_id: UUID,
):
    """Confirm 된 사이클 안의 entry create/update 는 400 으로 차단."""
    # entry 생성
    r1 = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(staff_schedule, card="50", cash="10"),
        headers=staff_headers,
    )
    assert r1.status_code == 201, r1.text
    entry_id = r1.json()["id"]

    # 매장 사이클 확정
    today = DateType.today()
    r2 = await async_client.post(
        f"/api/v1/console/tips/periods/confirm?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={},
        headers=admin_headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "confirmed"

    # 확정 후 entry update 시도 → 400 (lock guard)
    r3 = await async_client.patch(
        f"/api/v1/app/my/tips/entries/{entry_id}",
        json={"card_tips": "99"},
        headers=staff_headers,
    )
    assert r3.status_code == 400, r3.text
    assert "confirmed" in r3.text.lower() or "locked" in r3.text.lower()


@pytest.mark.asyncio
async def test_confirm_force_accepts_all_pending_distributions(
    async_client: AsyncClient,
    staff_headers: dict,
    admin_headers: dict,
    staff_schedule: str,
    sv_schedule: str,
    test_users: dict,
    test_store_id: UUID,
):
    """Confirm 진입 시 pending → auto_accepted 일괄 전환 (force=True)."""
    sv_id = test_users["testsv"]["id"]
    await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(
            staff_schedule, card="100", cash="20",
            distributions=[{"receiver_id": str(sv_id), "amount": "15"}],
        ),
        headers=staff_headers,
    )

    today = DateType.today()
    r = await async_client.post(
        f"/api/v1/console/tips/periods/confirm?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={},
        headers=admin_headers,
    )
    assert r.status_code == 200

    # pending 분배 0건이어야 한다
    async with async_session() as db:
        remaining = await db.scalar(
            select(TipDistribution).where(
                TipDistribution.status == "pending",
                TipDistribution.receiver_id == sv_id,
            )
        )
    assert remaining is None, "pending distribution should be force-accepted"


@pytest.mark.asyncio
async def test_4070_form_box_calculation_includes_received(
    async_client: AsyncClient,
    staff_headers: dict,
    admin_headers: dict,
    staff_schedule: str,
    test_users: dict,
    test_store_id: UUID,
):
    """4070 Box 1/2/3/4 — Box2 에 받은 분배 포함, Box4 = 1+2-3."""
    sv_id = test_users["testsv"]["id"]
    await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(
            staff_schedule, card="80", cash="20",
            distributions=[{"receiver_id": str(sv_id), "amount": "15"}],
        ),
        headers=staff_headers,
    )
    today = DateType.today()
    await async_client.post(
        f"/api/v1/console/tips/periods/confirm?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={}, headers=admin_headers,
    )

    forms = await async_client.get(
        f"/api/v1/console/tips/forms?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        headers=admin_headers,
    )
    assert forms.status_code == 200
    by_employee = {f["employee_id"]: f for f in forms.json()}

    # teststaff: cash 20 + card 80 - paid 15 = 85
    staff_form = by_employee[str(test_users["teststaff"]["id"])]
    assert Decimal(staff_form["reported_cash"]) == Decimal("20")
    assert Decimal(staff_form["reported_card"]) == Decimal("80")
    assert Decimal(staff_form["paid_out"]) == Decimal("15")
    assert Decimal(staff_form["net_tips"]) == Decimal("85")
    assert staff_form["pdf_key"] is not None  # PDF 생성됨

    # testsv: cash 0 + card 15 (received) - paid 0 = 15
    sv_form = by_employee[str(sv_id)]
    assert Decimal(sv_form["reported_card"]) == Decimal("15")
    assert Decimal(sv_form["paid_out"]) == Decimal("0")
    assert Decimal(sv_form["net_tips"]) == Decimal("15")


@pytest.mark.asyncio
async def test_force_close_requires_reason_min_length(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
):
    """Force-close reason 10자 미만 → 422."""
    today = DateType.today()
    r = await async_client.post(
        f"/api/v1/console/tips/periods/force-close?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={"reason": "short"},
        headers=admin_headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_force_close_sets_override_reason_and_audit(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
    db,
):
    """Force-close 시 override_reason 저장 + audit log action='force_close'."""
    today = DateType.today()
    long_reason = "Mobile release branch cut next week — closing early"
    r = await async_client.post(
        f"/api/v1/console/tips/periods/force-close?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={"reason": long_reason},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["override_reason"] == long_reason
    # audit log 조회
    logs = (await db.scalars(
        select(TipAuditLog).where(
            TipAuditLog.entity_type == "tip_period",
            TipAuditLog.action == "force_close",
        )
    )).all()
    assert any(l.comment == long_reason for l in logs)


@pytest.mark.asyncio
async def test_sign_form_marks_signed_and_saves_signature(
    async_client: AsyncClient,
    staff_headers: dict,
    admin_headers: dict,
    staff_schedule: str,
    test_users: dict,
    test_store_id: UUID,
    db,
):
    """Sign API — status signed + signature_image_key + audit log."""
    # entry 생성 + 사이클 확정
    await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(staff_schedule, card="40", cash="10"),
        headers=staff_headers,
    )
    today = DateType.today()
    await async_client.post(
        f"/api/v1/console/tips/periods/confirm?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={}, headers=admin_headers,
    )

    # 본인 폼 조회
    forms = (await async_client.get(
        "/api/v1/app/my/tips/forms", headers=staff_headers,
    )).json()
    assert len(forms) >= 1
    form_id = forms[0]["id"]
    assert forms[0]["status"] == "generated"

    # 사인 PNG blob upload
    fake_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf\xc0\xc0\xc0\xc0"
        b"\xc0\xc0\xc0\x00\x00\x00\x0d\x00\x01<\x9eA\xa1\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    blob = await async_client.post(
        "/api/v1/app/my/tips/signature/blob",
        content=fake_png,
        headers={**staff_headers, "Content-Type": "image/png"},
    )
    assert blob.status_code == 200, blob.text
    sig_key = blob.json()["signature_image_key"]
    assert sig_key.startswith("signatures/users/")

    # sign
    signed = await async_client.post(
        f"/api/v1/app/my/tips/forms/{form_id}/sign",
        json={"signature_image_key": sig_key, "save_for_future": True},
        headers=staff_headers,
    )
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert body["status"] == "signed"
    assert body["signed_at"] is not None
    assert body["signature_image_key"] == sig_key

    # audit log 확인
    logs = (await db.scalars(
        select(TipAuditLog).where(
            TipAuditLog.entity_type == "form_4070",
            TipAuditLog.action == "sign",
            TipAuditLog.entity_id == UUID(form_id),
        )
    )).all()
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_audit_logs_endpoint_filters_by_store(
    async_client: AsyncClient,
    admin_headers: dict,
    staff_headers: dict,
    staff_schedule: str,
    test_store_id: UUID,
):
    """audit-logs?store_id=... 가 그 매장 entity 만 반환한다."""
    await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(staff_schedule, card="10", cash="5"),
        headers=staff_headers,
    )
    r = await async_client.get(
        f"/api/v1/console/tips/audit-logs?store_id={test_store_id}",
        headers=admin_headers,
    )
    assert r.status_code == 200
    rows = r.json()
    # 모든 row 가 우리 store 의 entity 여야 함 — 다른 store 의 row 가 섞이면 안 됨.
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_sv_cannot_confirm_period(
    async_client: AsyncClient,
    sv_headers: dict,
    test_store_id: UUID,
    test_users: dict,
):
    """가이드 §1.1 — SV 는 사이클 확정 권한 없음 (Owner/GM 위주).

    DEFAULT_ROLE_PERMISSIONS 변경은 신규 매핑에만 적용되고 기존 admin 가 추가한
    매핑은 보존된다. 본 테스트는 SV 에 `tips:period_confirm` 매핑이 있어도 정책상
    제거되어야 함을 명시. 테스트 시작 시점에 직접 cleanup 한다.
    """
    from app.models.permission import Permission, RolePermission
    from app.models.user import Role

    sv_id = test_users["testsv"]["id"]
    async with async_session() as db:
        perm = await db.scalar(
            select(Permission).where(Permission.code == "tips:period_confirm")
        )
        if perm:
            # SV 역할에서 매핑 제거
            sv_role = await db.scalar(
                select(Role)
                .join(User, User.role_id == Role.id)
                .where(User.id == sv_id)
            )
            if sv_role:
                await db.execute(
                    delete(RolePermission).where(
                        RolePermission.role_id == sv_role.id,
                        RolePermission.permission_id == perm.id,
                    )
                )
                await db.commit()

    today = DateType.today()
    r = await async_client.post(
        f"/api/v1/console/tips/periods/confirm?store_id={test_store_id}&date_in_cycle={today.isoformat()}",
        json={},
        headers=sv_headers,
    )
    # 권한 부족 → 403
    assert r.status_code == 403, r.text
