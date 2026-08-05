"""Integration — pay stub PDF API (Phase 4, E4).

대상: app/api/console/payroll.py (entries/{id}/stub) + app/services/pay_stub_service.py
    - generate: files(상대경로 key, URL 아님) + file_usages(owner_type='pay_stub') 등록
    - 재생성 멱등: files/file_usages 행 증식 없음, 같은 파일 재사용
    - download: 인증 엔드포인트가 PDF 바이트 직접 서빙 (%PDF- 매직)
    - 404 (없는 entry / 미생성 stub), 409 (미확정 기간 방어 가드)
    - 권한: generate=payroll:export, download=payroll:read (GM 거부)

파일 레지스트리 규칙 (2026-06-24): path UNIQUE, usage-only 삭제 — 스텁은
entry 기반 고정 key(payroll/stubs/{period}/{entry}.pdf) 로 덮어쓴다.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.file import File, FileUsage
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store
from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent
from app.models.tip import TipEntry, TipPeriod
from app.models.user import User
from app.schemas.payroll import CALC_VERSION
from app.services.pay_stub_service import STUB_OWNER_TYPE, pay_stub_service
from app.services.payroll_period_service import payroll_period_service
from app.services.storage_service import storage_service
from app.utils.jwt import create_access_token

_BASE = "/api/v1/console/payroll"

_JUL_MID = date(2026, 7, 10)
_MON = date(2026, 7, 6)
_RATE = Decimal("20.00")


@pytest_asyncio.fixture
async def stub_ctx(
    seed_organization: dict, seed_roles: dict[str, UUID]
) -> AsyncIterator[dict]:
    """throwaway store + 직원 1명 (empid/crewid 스냅샷 검증용). 종료 시 전부 정리."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    crewid = 910_000 + int(uuid_mod.uuid4().hex[:4], 16)
    async with async_session() as db:
        store = Store(
            organization_id=org_id,
            name=f"__pay_stub_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
            default_hourly_rate=_RATE,
            address="456 Stub Ave, Los Angeles, CA",
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)

        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__pay_stub_{suffix}",
            full_name="Stub Employee",
            password_hash="x",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        member = OrgMember(
            user_id=user.id,
            organization_id=org_id,
            role_id=seed_roles["staff"],
            crewid=crewid,
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        db.add(OrgMemberStore(org_member_id=member.id, store_id=store.id, empid=42))
        await db.commit()

    ctx = {
        "org_id": org_id,
        "store_id": store.id,
        "user_id": user.id,
        "member_id": member.id,
        "crewid": crewid,
    }
    yield ctx

    async with async_session() as db:
        # stub 파일 레지스트리 + blob 정리 (period 기반 고정 key)
        period_ids = (
            await db.scalars(
                select(PayPeriod.id).where(PayPeriod.store_id == store.id)
            )
        ).all()
        for pid in period_ids:
            files = (
                await db.scalars(
                    select(File).where(File.path.like(f"payroll/stubs/{pid}/%"))
                )
            ).all()
            for f in files:
                await db.execute(
                    delete(FileUsage).where(FileUsage.file_id == f.id)
                )
                storage_service.delete_file(f.path)
                await db.delete(f)
        await db.execute(
            delete(PayrollEvent).where(PayrollEvent.store_id == store.id)
        )
        await db.execute(
            delete(PayrollEntry).where(PayrollEntry.store_id == store.id)
        )
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == store.id))
        await db.execute(delete(TipEntry).where(TipEntry.store_id == store.id))
        await db.execute(delete(TipPeriod).where(TipPeriod.store_id == store.id))
        await db.execute(delete(Attendance).where(Attendance.store_id == store.id))
        await db.execute(
            delete(OrgMemberStore).where(OrgMemberStore.org_member_id == member.id)
        )
        await db.execute(delete(OrgMember).where(OrgMember.id == member.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.execute(delete(Store).where(Store.id == store.id))
        await db.commit()


async def _confirmed_entry_id(
    async_client: AsyncClient, admin_headers: dict, ctx: dict
) -> str:
    """Mon 10h 근무 + confirmed tip period → confirm 플로우로 동결 entry 생성."""
    async with async_session() as db:
        db.add(
            Attendance(
                organization_id=ctx["org_id"],
                store_id=ctx["store_id"],
                user_id=ctx["user_id"],
                work_date=_MON,
                status="clocked_out",
                total_work_minutes=600,  # 10h → OT + meal/rest penalty (사유 라인)
            )
        )
        db.add(
            TipPeriod(
                store_id=ctx["store_id"],
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 15),
                status="confirmed",
            )
        )
        period = await payroll_period_service.ensure_period(
            db, store_id=ctx["store_id"], date_in_period=_JUL_MID
        )
        await db.commit()
        period_id = str(period.id)

    resp = await async_client.post(
        f"{_BASE}/periods/{period_id}/confirm", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["calc_version"] == CALC_VERSION
    return entries[0]["id"]


async def _login(username: str) -> dict[str, str]:
    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        token = create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Generate — 레지스트리 등록 + 멱등
# ---------------------------------------------------------------------------


async def test_generate_registers_file_and_usage(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """generate → File(상대경로 key) + FileUsage(owner_type='pay_stub') 1건씩."""
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)

    resp = await async_client.post(
        f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry_id"] == entry_id
    assert body["filename"].startswith("PayStub_StubEmployee_")
    assert body["filename"].endswith(".pdf")
    assert body["size_bytes"] > 2000
    # 경로/URL 미노출 (IDOR 방지 — 다운로드는 인증 GET 전용)
    assert "path" not in body and "url" not in body

    async with async_session() as db:
        usage = (
            await db.execute(
                select(FileUsage).where(
                    FileUsage.owner_type == STUB_OWNER_TYPE,
                    FileUsage.owner_id == UUID(entry_id),
                )
            )
        ).scalar_one()
        file = await db.get(File, usage.file_id)
        assert str(file.id) == body["file_id"]
        # 상대경로 key 만 저장 — 절대 URL 금지 (Decision #7)
        assert file.path == f"payroll/stubs/{body['pay_period_id']}/{entry_id}.pdf"
        assert "://" not in file.path and not file.path.startswith("/")
        assert file.file_type == "document"
        assert file.mime_type == "application/pdf"
        assert file.organization_id == stub_ctx["org_id"]
        assert file.store_id == stub_ctx["store_id"]
        assert file.size_bytes == body["size_bytes"]
    # blob 실재 확인 (로컬 버킷)
    assert storage_service.read_bytes(
        f"payroll/stubs/{body['pay_period_id']}/{entry_id}.pdf"
    ).startswith(b"%PDF-")


async def test_regenerate_is_idempotent(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """재생성 — 같은 File 행 재사용, usage 1건 유지 (행 증식 없음)."""
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)

    r1 = await async_client.post(
        f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
    )
    r2 = await async_client.post(
        f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["file_id"] == r2.json()["file_id"]

    async with async_session() as db:
        usages = (
            await db.scalars(
                select(FileUsage).where(
                    FileUsage.owner_type == STUB_OWNER_TYPE,
                    FileUsage.owner_id == UUID(entry_id),
                )
            )
        ).all()
        assert len(usages) == 1
        files = (
            await db.scalars(
                select(File).where(
                    File.path.like(f"payroll/stubs/{r1.json()['pay_period_id']}/%")
                )
            )
        ).all()
        assert len(files) == 1


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def test_download_serves_pdf_bytes(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)
    gen = await async_client.post(
        f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
    )
    assert gen.status_code == 200

    resp = await async_client.get(
        f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert "PayStub_StubEmployee_" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")
    assert len(resp.content) == gen.json()["size_bytes"]


async def test_download_before_generate_404_actionable(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """미생성 stub 다운로드 — 404 + 다음 행동(generate) 안내."""
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)
    resp = await async_client.get(
        f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
    )
    assert resp.status_code == 404
    assert "generate it first" in resp.json()["detail"]


async def test_unknown_entry_404(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    missing = uuid_mod.uuid4()
    resp = await async_client.post(
        f"{_BASE}/entries/{missing}/stub", headers=admin_headers
    )
    assert resp.status_code == 404
    resp2 = await async_client.get(
        f"{_BASE}/entries/{missing}/stub", headers=admin_headers
    )
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# 409 — 미확정 기간 방어 가드 (entry 는 confirm 시에만 생기지만 방어)
# ---------------------------------------------------------------------------


async def test_open_period_entry_409(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """open 기간에 (비정상 경로로) entry 가 있어도 stub 은 409 로 거부."""
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=stub_ctx["store_id"], date_in_period=_JUL_MID
        )
        entry = PayrollEntry(
            pay_period_id=period.id,
            organization_id=stub_ctx["org_id"],
            store_id=stub_ctx["store_id"],
            user_id=stub_ctx["user_id"],
            member_name="Stub Employee",
            calc_version=CALC_VERSION,
            breakdown={"calc_version": CALC_VERSION, "segments": [], "days": [],
                       "penalties": [], "tip_period_id": None, "sources": None},
        )
        db.add(entry)
        await db.commit()
        await db.refresh(entry)
        entry_id = str(entry.id)
        assert period.status == "open"

    for method in ("post", "get"):
        resp = await getattr(async_client, method)(
            f"{_BASE}/entries/{entry_id}/stub", headers=admin_headers
        )
        assert resp.status_code == 409, resp.text
        assert "confirmed pay periods" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 권한 — GM 은 payroll:export / payroll:read 미보유 (Owner 전용)
# ---------------------------------------------------------------------------


async def test_gm_denied_on_stub_endpoints(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict,
    _clean_state,
) -> None:
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)
    headers = await _login("testgm")

    resp = await async_client.post(
        f"{_BASE}/entries/{entry_id}/stub", headers=headers
    )
    assert resp.status_code == 403
    assert "payroll:export" in resp.json()["detail"]

    resp2 = await async_client.get(
        f"{_BASE}/entries/{entry_id}/stub", headers=headers
    )
    assert resp2.status_code == 403
    assert "payroll:read" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# 서비스 단위 — stub_key / filename 규칙
# ---------------------------------------------------------------------------


async def test_stub_key_and_filename_scheme(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)
    async with async_session() as db:
        entry = await db.get(PayrollEntry, UUID(entry_id))
        period = await db.get(PayPeriod, entry.pay_period_id)
        assert pay_stub_service.stub_key(entry) == (
            f"payroll/stubs/{entry.pay_period_id}/{entry.id}.pdf"
        )
        assert pay_stub_service.stub_filename(entry, period) == (
            "PayStub_StubEmployee_2026-07-01~2026-07-15.pdf"
        )
        assert pay_stub_service.stub_filename(entry, period, draft=True) == (
            "PayStub_StubEmployee_2026-07-01~2026-07-15_DRAFT.pdf"
        )


# ---------------------------------------------------------------------------
# Draft stub — open 기간, preview 기반 즉석 생성 (비영속)
# ---------------------------------------------------------------------------


async def _open_period_id(ctx: dict) -> str:
    """근무 1건 시딩 + open 기간 보장 (confirm 하지 않음)."""
    async with async_session() as db:
        db.add(
            Attendance(
                organization_id=ctx["org_id"],
                store_id=ctx["store_id"],
                user_id=ctx["user_id"],
                work_date=_MON,
                status="clocked_out",
                total_work_minutes=600,
            )
        )
        period = await payroll_period_service.ensure_period(
            db, store_id=ctx["store_id"], date_in_period=_JUL_MID
        )
        await db.commit()
        return str(period.id)


async def test_draft_stub_open_period(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """open 기간 draft: POST 메타(_DRAFT 파일명) + GET PDF 바이트, 저장 없음."""
    period_id = await _open_period_id(stub_ctx)
    url = f"{_BASE}/periods/{period_id}/users/{stub_ctx['user_id']}/stub"

    resp = await async_client.post(url, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["filename"].endswith("_DRAFT.pdf")
    assert meta["size_bytes"] > 1000

    resp = await async_client.get(url, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")
    assert "_DRAFT.pdf" in resp.headers["content-disposition"]

    # 비영속 — files 레지스트리에 아무것도 안 남는다
    async with async_session() as db:
        files = (
            await db.scalars(
                select(File).where(File.path.like(f"payroll/stubs/{period_id}/%"))
            )
        ).all()
        assert files == []


async def test_draft_stub_confirmed_period_409(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """confirmed 기간에서 draft 요청 → 409 (entry stub 안내)."""
    entry_id = await _confirmed_entry_id(async_client, admin_headers, stub_ctx)
    async with async_session() as db:
        entry = await db.get(PayrollEntry, UUID(entry_id))
        period_id = str(entry.pay_period_id)
    url = f"{_BASE}/periods/{period_id}/users/{stub_ctx['user_id']}/stub"
    resp = await async_client.post(url, headers=admin_headers)
    assert resp.status_code == 409
    assert "frozen entry" in resp.json()["detail"]


async def test_draft_stub_unknown_user_404(
    async_client: AsyncClient, admin_headers: dict, stub_ctx: dict
) -> None:
    """이 기간에 payroll 데이터 없는 직원 → 404."""
    period_id = await _open_period_id(stub_ctx)
    url = f"{_BASE}/periods/{period_id}/users/{uuid_mod.uuid4()}/stub"
    resp = await async_client.post(url, headers=admin_headers)
    assert resp.status_code == 404
