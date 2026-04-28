"""GET /app/auth/stores/by-code/{encoded} 엔드포인트 테스트."""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.url_encoding import encode_uuid
from app.database import async_session
from app.main import app
from app.models.organization import Organization, Store


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def http() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def signup_store():
    """전용 테스트 매장 생성 (cleanup 보장)."""
    async with async_session() as db:
        org = (await db.execute(select(Organization).order_by(Organization.created_at).limit(1))).scalar_one()
        store = Store(
            organization_id=org.id,
            name="__signup_link_test__",
            address="123 Test Way",
            timezone="UTC",
            is_active=True,
            accepting_signups=True,
            cover_photos=[],
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        store_id = store.id
        org_code = org.code
        org_name = org.name

    try:
        yield {
            "id": store_id,
            "encoded": encode_uuid(store_id),
            "org_code": org_code,
            "org_name": org_name,
        }
    finally:
        async with async_session() as db:
            await db.execute(
                Store.__table__.delete().where(Store.id == store_id)
            )
            await db.commit()


async def test_returns_store_and_organization_for_active_link(
    http: AsyncClient, signup_store
):
    res = await http.get(f"/api/v1/app/auth/stores/by-code/{signup_store['encoded']}")
    assert res.status_code == 200
    body = res.json()

    assert body["store"]["id"] == str(signup_store["id"])
    assert body["store"]["name"] == "__signup_link_test__"
    assert body["store"]["address"] == "123 Test Way"
    assert body["store"]["cover_photos"] == []

    assert body["organization"]["company_code"] == signup_store["org_code"]
    assert body["organization"]["name"] == signup_store["org_name"]


async def test_invalid_encoded_returns_404_invalid_link(http: AsyncClient):
    res = await http.get("/api/v1/app/auth/stores/by-code/!!!!!")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "invalid_link"


async def test_short_encoded_returns_404_invalid_link(http: AsyncClient):
    # decode_uuid raises ValueError → 404 invalid_link
    res = await http.get("/api/v1/app/auth/stores/by-code/AAAAAAAAAAA")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "invalid_link"


async def test_unknown_store_returns_404_store_not_found(http: AsyncClient):
    encoded = encode_uuid(uuid.uuid4())  # random UUID, store doesn't exist
    res = await http.get(f"/api/v1/app/auth/stores/by-code/{encoded}")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "store_not_found"


async def test_inactive_store_returns_404_store_not_found(
    http: AsyncClient, signup_store
):
    async with async_session() as db:
        store = (await db.execute(select(Store).where(Store.id == signup_store["id"]))).scalar_one()
        store.is_active = False
        await db.commit()

    res = await http.get(f"/api/v1/app/auth/stores/by-code/{signup_store['encoded']}")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "store_not_found"


async def test_paused_store_returns_404_signups_paused(
    http: AsyncClient, signup_store
):
    async with async_session() as db:
        store = (await db.execute(select(Store).where(Store.id == signup_store["id"]))).scalar_one()
        store.accepting_signups = False
        await db.commit()

    res = await http.get(f"/api/v1/app/auth/stores/by-code/{signup_store['encoded']}")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "signups_paused"


async def test_no_auth_required(http: AsyncClient, signup_store):
    """공개 endpoint — Authorization 헤더 없이 200."""
    res = await http.get(
        f"/api/v1/app/auth/stores/by-code/{signup_store['encoded']}",
        headers={},
    )
    assert res.status_code == 200
