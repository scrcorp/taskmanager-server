"""API integration — console storage 진입점 보안/검증 (Phase 1 통합).

대상:
  - POST /api/v1/console/storage/presigned-url  → folder allowlist 검증
  - PUT  /api/v1/console/storage/upload/{key}    → 키 안전성 검증 (raw PUT)

통합 후 클라이언트가 임의 폴더/임의 경로로 쓰는 것을 서버가 거부함을 보장한다.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.services import storage_service as ss
from app.services.storage_service import storage_service

pytestmark = pytest.mark.asyncio


async def test_presigned_url_good_folder(
    async_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await async_client.post(
        "/api/v1/console/storage/presigned-url",
        json={"filename": "a.jpg", "content_type": "image/jpeg", "folder": "reviews"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upload_url"] and body["file_url"]


async def test_presigned_url_rejects_unregistered_folder(
    async_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await async_client.post(
        "/api/v1/console/storage/presigned-url",
        json={"filename": "a.jpg", "content_type": "image/jpeg", "folder": "evil_folder"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_folder"


async def test_put_upload_rejects_unsafe_key(async_client: AsyncClient) -> None:
    # temp/ 등 허용 접두사가 아닌 키 → 400 (임의경로 쓰기 차단)
    resp = await async_client.put(
        "/api/v1/console/storage/upload/evil/x.bin",
        content=b"payload",
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_key"


async def test_put_upload_accepts_temp_key(async_client: AsyncClient) -> None:
    key = f"temp/reviews/test/{uuid.uuid4().hex}.bin"
    path = ss.BUCKET_DIR / key
    try:
        resp = await async_client.put(
            f"/api/v1/console/storage/upload/{key}", content=b"payload"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        if storage_service.is_local:
            assert path.exists()
    finally:
        if path.exists():
            path.unlink()
