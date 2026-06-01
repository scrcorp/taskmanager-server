"""GET /console/hiring/applications — cross-store aggregate (Inbox) 테스트.

스코프 자동 한정, 필터(store/stage/q), 정렬, 페이지네이션, counts 검증.

핵심 보안 케이스: GM 은 관리(is_manager) 매장 지원자만 보고 타 매장은 누수되지 않는다.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.hiring import Application, Candidate
from app.models.user_store import UserStore
from app.utils.password import hash_password

URL = "/api/v1/console/hiring/applications"
PW_HASH = hash_password("1234")


async def _login(username: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/console/auth/login",
            json={"username": username, "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _mk_candidate(tag: str, full_name: str) -> Candidate:
    nonce = uuid.uuid4().hex[:8]
    email = f"__hire_{tag}_{nonce}@test.local"
    return Candidate(
        username=f"__hire_{tag}_{nonce}",
        email=email,
        email_normalized=email.lower(),
        password_hash=PW_HASH,
        full_name=full_name,
    )


@pytest_asyncio.fixture
async def seeded_apps(test_store_id: UUID, second_store_id: UUID):
    """store A(test_store_id) 3건 + store B(second_store_id) 2건 지원자 시드.

    A: new / reviewing / interview, B: new / hired. 테스트 후 모두 정리.
    """
    created_candidate_ids: list[UUID] = []
    created_app_ids: list[UUID] = []
    async with async_session() as db:
        plan = [
            (test_store_id, "a1", "Alice Anderson", "new"),
            (test_store_id, "a2", "Aaron Avery", "reviewing"),
            (test_store_id, "a3", "Amy Adams", "interview"),
            (second_store_id, "b1", "Bob Brown", "new"),
            (second_store_id, "b2", "Bella Banks", "hired"),
        ]
        for store_id, tag, name, stage in plan:
            cand = _mk_candidate(tag, name)
            db.add(cand)
            await db.flush()
            created_candidate_ids.append(cand.id)
            app_row = Application(candidate_id=cand.id, store_id=store_id, stage=stage)
            db.add(app_row)
            await db.flush()
            created_app_ids.append(app_row.id)
        await db.commit()

    yield {"store_a": test_store_id, "store_b": second_store_id}

    async with async_session() as db:
        await db.execute(delete(Application).where(Application.id.in_(created_app_ids)))
        await db.execute(delete(Candidate).where(Candidate.id.in_(created_candidate_ids)))
        await db.commit()


@pytest_asyncio.fixture
async def gm_managing_a(test_users: dict, test_store_id: UUID):
    """testgm 을 store A 의 매니저(is_manager=True)로 배정. 테스트 후 해제."""
    gm_id: UUID = test_users["testgm"]["id"]
    async with async_session() as db:
        existing = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == gm_id, UserStore.store_id == test_store_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(user_id=gm_id, store_id=test_store_id, is_manager=True))
        else:
            existing.is_manager = True
        await db.commit()

    yield gm_id

    async with async_session() as db:
        await db.execute(
            delete(UserStore).where(
                UserStore.user_id == gm_id, UserStore.store_id == test_store_id
            )
        )
        await db.commit()


# ────────────────────────────────────────────────────────────────
# Owner — 조직 전체 매장 가로질러 조회
# ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_owner_sees_all_stores(async_client: AsyncClient, admin_headers, seeded_apps):
    resp = await async_client.get(URL, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    store_ids = {item["store"]["id"] for item in body["items"]}
    assert str(seeded_apps["store_a"]) in store_ids
    assert str(seeded_apps["store_b"]) in store_ids
    # 각 항목에 store 정보가 붙어 있어야 함
    assert all({"id", "name", "code"} <= set(item["store"].keys()) for item in body["items"])
    assert body["total"] >= 5


@pytest.mark.asyncio
async def test_counts_are_stage_independent(async_client: AsyncClient, admin_headers, seeded_apps):
    """stage=active 필터를 줘도 counts 는 전체 단계 집계를 반환."""
    resp = await async_client.get(URL, params={"stage": "active"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # items 는 active(new/reviewing/interview)만
    assert all(it["stage"] in ("new", "reviewing", "interview") for it in body["items"])
    # counts 에는 hired 도 집계됨 (store B 의 Bella)
    assert body["counts"]["hired"] >= 1


# ────────────────────────────────────────────────────────────────
# GM — 관리 매장만 (IDOR 누수 방지)
# ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gm_scoped_to_managed_store(
    async_client: AsyncClient, seeded_apps, gm_managing_a
):
    token = await _login("testgm")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await async_client.get(URL, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    store_ids = {item["store"]["id"] for item in body["items"]}
    # store A 만 보이고 store B 는 누수 없음
    assert store_ids == {str(seeded_apps["store_a"])}
    assert all(it["store"]["id"] == str(seeded_apps["store_a"]) for it in body["items"])
    # counts 도 관리 매장 기준 (B 의 hired 는 안 잡힘)
    assert body["counts"]["hired"] == 0


@pytest.mark.asyncio
async def test_gm_store_filter_other_store_forbidden(
    async_client: AsyncClient, seeded_apps, gm_managing_a
):
    """GM 이 접근 불가 매장(store B)으로 store_id 필터하면 403."""
    token = await _login("testgm")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await async_client.get(
        URL, params={"store_id": str(seeded_apps["store_b"])}, headers=headers
    )
    assert resp.status_code == 403, resp.text


# ────────────────────────────────────────────────────────────────
# 필터 / 정렬 / 페이지네이션
# ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_store_filter(async_client: AsyncClient, admin_headers, seeded_apps):
    resp = await async_client.get(
        URL, params={"store_id": str(seeded_apps["store_b"])}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {it["store"]["id"] for it in body["items"]} == {str(seeded_apps["store_b"])}


@pytest.mark.asyncio
async def test_stage_filter_specific(async_client: AsyncClient, admin_headers, seeded_apps):
    resp = await async_client.get(URL, params={"stage": "interview"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert all(it["stage"] == "interview" for it in body["items"])
    assert body["total"] >= 1


@pytest.mark.asyncio
async def test_search_q(async_client: AsyncClient, admin_headers, seeded_apps):
    resp = await async_client.get(URL, params={"q": "Bella"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert all("bella" in it["candidate"]["full_name"].lower() for it in body["items"])


@pytest.mark.asyncio
async def test_pagination(async_client: AsyncClient, admin_headers, seeded_apps):
    resp = await async_client.get(URL, params={"per_page": 2, "page": 1}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) <= 2
    assert body["per_page"] == 2
    assert body["page"] == 1
    assert body["pages"] == (body["total"] + 1) // 2
