"""Admin store hiring 엔드포인트 테스트.

- PATCH /admin/stores/{id}/accepting-signups
- GET / POST / PATCH / DELETE /admin/stores/{id}/cover-photos
"""

from __future__ import annotations

import io
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

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
async def hiring_store():
    async with async_session() as db:
        org = (await db.execute(select(Organization).order_by(Organization.created_at).limit(1))).scalar_one()
        store = Store(
            organization_id=org.id,
            name="__hiring_test_store__",
            timezone="UTC",
            is_active=True,
            accepting_signups=True,
            cover_photos=[],
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        store_id = store.id

    try:
        yield store_id
    finally:
        async with async_session() as db:
            await db.execute(Store.__table__.delete().where(Store.id == store_id))
            await db.commit()


def _png_bytes() -> bytes:
    """1x1 transparent PNG."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
        b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc"
        b"\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )


async def test_accepting_signups_toggle(http, admin_headers, hiring_store):
    res = await http.patch(
        f"/api/v1/admin/stores/{hiring_store}/accepting-signups",
        json={"accepting_signups": False},
        headers=admin_headers,
    )
    assert res.status_code == 200
    assert res.json()["accepting_signups"] is False

    res = await http.patch(
        f"/api/v1/admin/stores/{hiring_store}/accepting-signups",
        json={"accepting_signups": True},
        headers=admin_headers,
    )
    assert res.json()["accepting_signups"] is True


async def test_list_cover_photos_empty(http, admin_headers, hiring_store):
    res = await http.get(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
    )
    assert res.status_code == 200
    assert res.json() == []


async def test_upload_cover_photo_first_is_primary(http, admin_headers, hiring_store):
    res = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert res.status_code == 201, res.text
    photo = res.json()
    assert photo["is_primary"] is True
    assert photo["url"] is not None
    assert photo["size"] > 0
    assert len(photo["id"]) == 12


async def test_upload_rejects_invalid_content_type(http, admin_headers, hiring_store):
    res = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("photo.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_file_type"


async def test_upload_rejects_too_large(http, admin_headers, hiring_store):
    big = b"\x00" * (5 * 1024 * 1024 + 1)
    res = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("big.png", big, "image/png")},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "file_too_large"


async def test_set_primary_changes_only_one(http, admin_headers, hiring_store):
    # 두 장 업로드
    r1 = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    r2 = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("b.png", _png_bytes(), "image/png")},
    )
    p1 = r1.json()
    p2 = r2.json()
    assert p1["is_primary"] is True
    assert p2["is_primary"] is False

    # p2를 primary로 변경
    res = await http.patch(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos/{p2['id']}/primary",
        headers=admin_headers,
    )
    assert res.status_code == 200

    listed = (
        await http.get(
            f"/api/v1/admin/stores/{hiring_store}/cover-photos",
            headers=admin_headers,
        )
    ).json()
    assert {p["id"]: p["is_primary"] for p in listed} == {p1["id"]: False, p2["id"]: True}


async def test_set_primary_rejects_unknown_id(http, admin_headers, hiring_store):
    res = await http.patch(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos/abc123abc123/primary",
        headers=admin_headers,
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "photo_not_found"


async def test_delete_promotes_next_to_primary(http, admin_headers, hiring_store):
    r1 = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    r2 = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("b.png", _png_bytes(), "image/png")},
    )
    primary_id = r1.json()["id"]
    other_id = r2.json()["id"]

    res = await http.delete(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos/{primary_id}",
        headers=admin_headers,
    )
    assert res.status_code == 204

    listed = (
        await http.get(
            f"/api/v1/admin/stores/{hiring_store}/cover-photos",
            headers=admin_headers,
        )
    ).json()
    assert len(listed) == 1
    assert listed[0]["id"] == other_id
    assert listed[0]["is_primary"] is True


async def test_delete_last_photo_leaves_empty_list(http, admin_headers, hiring_store):
    r = await http.post(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos",
        headers=admin_headers,
        files={"file": ("solo.png", _png_bytes(), "image/png")},
    )
    photo_id = r.json()["id"]

    await http.delete(
        f"/api/v1/admin/stores/{hiring_store}/cover-photos/{photo_id}",
        headers=admin_headers,
    )

    listed = (
        await http.get(
            f"/api/v1/admin/stores/{hiring_store}/cover-photos",
            headers=admin_headers,
        )
    ).json()
    assert listed == []
