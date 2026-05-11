"""SV 의 checklist-instances 매장 권한 테스트.

testsv 는 test_store_id 에만 user_stores 연결되어 있고,
second_store_id 의 checklist instance 는:
  - list 응답에 포함되지 않아야 함
  - 명시적 store_id 필터로 요청 시 403
  - 단건 GET / by-schedule / patch / review 등 직접 접근 시 403
testadmin (Owner) 은 두 매장 모두 접근 가능해야 함.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.checklist import ChecklistInstance
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sv_only_in_first_store(test_users, test_store_id, second_store_id):
    """testsv 가 test_store_id 에만 등록되도록 강제. second_store_id 등록은 제거."""
    sv_id: UUID = test_users["testsv"]["id"]
    async with async_session() as db:
        # second_store_id 등록 제거 (있으면)
        await db.execute(
            delete(UserStore).where(
                UserStore.user_id == sv_id,
                UserStore.store_id == second_store_id,
            )
        )
        # test_store_id 등록 보장
        existing = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == sv_id,
                    UserStore.store_id == test_store_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(user_id=sv_id, store_id=test_store_id, is_manager=False))
        await db.commit()
    yield


@pytest_asyncio.fixture
async def two_store_instances(test_users, test_store_id, second_store_id):
    """test_store_id, second_store_id 각각에 ChecklistInstance 1개씩 생성."""
    org_id = test_users["testadmin"]["organization_id"]
    user_id = test_users["testadmin"]["id"]
    today = date.today()

    created_ids: list[UUID] = []
    async with async_session() as db:
        for sid in (test_store_id, second_store_id):
            inst = ChecklistInstance(
                organization_id=org_id,
                store_id=sid,
                user_id=user_id,
                work_date=today,
                total_items=0,
                completed_items=0,
                status="pending",
            )
            db.add(inst)
            await db.flush()
            await db.refresh(inst)
            created_ids.append(inst.id)
        await db.commit()

    first_id, second_id = created_ids

    yield {"first": first_id, "second": second_id}

    async with async_session() as db:
        await db.execute(delete(ChecklistInstance).where(ChecklistInstance.id.in_(created_ids)))
        await db.commit()


async def _login(username: str, password: str = "1234") -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/console/auth/login",
            json={"username": username, "password": password},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def sv_headers() -> dict[str, str]:
    token = await _login("testsv")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sv_list_excludes_inaccessible_stores(
    async_client: AsyncClient,
    sv_headers: dict[str, str],
    sv_only_in_first_store,
    two_store_instances: dict[str, UUID],
    test_store_id: UUID,
    second_store_id: UUID,
):
    """SV list 응답엔 권한 있는 매장 인스턴스만 포함."""
    resp = await async_client.get(
        "/api/v1/console/checklist-instances",
        headers=sv_headers,
        params={"per_page": 100},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    store_ids = {item["store_id"] for item in items}
    assert str(test_store_id) in store_ids or len(items) == 0  # first store 인스턴스가 보일 수 있음
    assert str(second_store_id) not in store_ids


async def test_sv_explicit_store_filter_forbidden(
    async_client: AsyncClient,
    sv_headers: dict[str, str],
    sv_only_in_first_store,
    second_store_id: UUID,
):
    """SV 가 권한 없는 store_id 를 명시 → 403."""
    resp = await async_client.get(
        "/api/v1/console/checklist-instances",
        headers=sv_headers,
        params={"store_id": str(second_store_id)},
    )
    assert resp.status_code == 403, resp.text


async def test_sv_get_single_inaccessible_instance_forbidden(
    async_client: AsyncClient,
    sv_headers: dict[str, str],
    sv_only_in_first_store,
    two_store_instances: dict[str, UUID],
):
    """SV 가 권한 없는 매장의 instance 를 직접 GET → 403."""
    resp = await async_client.get(
        f"/api/v1/console/checklist-instances/{two_store_instances['second']}",
        headers=sv_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_sv_completion_log_explicit_store_forbidden(
    async_client: AsyncClient,
    sv_headers: dict[str, str],
    sv_only_in_first_store,
    second_store_id: UUID,
):
    """SV 가 completion-log 에 권한 없는 store_id → 403."""
    resp = await async_client.get(
        "/api/v1/console/checklist-instances/completion-log",
        headers=sv_headers,
        params={"store_id": str(second_store_id)},
    )
    assert resp.status_code == 403, resp.text


async def test_sv_review_summary_explicit_store_forbidden(
    async_client: AsyncClient,
    sv_headers: dict[str, str],
    sv_only_in_first_store,
    second_store_id: UUID,
):
    """SV 가 review-summary 에 권한 없는 store_id → 403."""
    resp = await async_client.get(
        "/api/v1/console/checklist-instances/review-summary",
        headers=sv_headers,
        params={"store_id": str(second_store_id)},
    )
    assert resp.status_code == 403, resp.text


async def test_admin_sees_both_stores(
    async_client: AsyncClient,
    admin_headers: dict[str, str],
    two_store_instances: dict[str, UUID],
    test_store_id: UUID,
    second_store_id: UUID,
):
    """testadmin (Owner) 은 두 매장 모두 보임 — sanity."""
    resp = await async_client.get(
        "/api/v1/console/checklist-instances",
        headers=admin_headers,
        params={"per_page": 100},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    store_ids = {item["store_id"] for item in items}
    assert str(test_store_id) in store_ids
    assert str(second_store_id) in store_ids
