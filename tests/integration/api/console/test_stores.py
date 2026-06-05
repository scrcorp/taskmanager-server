"""API integration tests — app/api/console/stores.py (accepting_signups).

대상:
    - GET   /api/v1/console/stores                          (list — accepting_signups 포함)
    - GET   /api/v1/console/stores/{store_id}               (detail — accepting_signups 포함)
    - PATCH /api/v1/console/stores/{store_id}/accepting-signups (토글 → GET 응답에 반영)

배경 (버그 회귀 방지):
    PATCH 는 DB 를 갱신했지만 StoreResponse/StoreDetailResponse 에
    accepting_signups 필드가 없어 console 이 항상 active 로 표시되던 버그.

[작성됨] — 이번 phase
- detail 응답에 accepting_signups 필드 존재
- list 응답의 모든 매장에 accepting_signups 필드 존재
- PATCH false → detail/list 에 false 반영
- PATCH true → 다시 true 반영 (재활성화 가능)

[작성 필요] — 추후
- store CRUD (create/update/delete) 일반 분기
- cover-photos 엔드포인트
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def restore_accepting_signups(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
):
    """테스트 후 accepting_signups 를 원래 값으로 복구."""
    resp = await async_client.get(
        f"/api/v1/console/stores/{test_store_id}", headers=admin_headers
    )
    original = resp.json().get("accepting_signups", True)
    yield
    await async_client.patch(
        f"/api/v1/console/stores/{test_store_id}/accepting-signups",
        headers=admin_headers,
        json={"accepting_signups": original},
    )


async def test_store_detail_includes_accepting_signups(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
) -> None:
    """detail 응답에 accepting_signups 가 bool 로 존재."""
    resp = await async_client.get(
        f"/api/v1/console/stores/{test_store_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "accepting_signups" in body
    assert isinstance(body["accepting_signups"], bool)


async def test_store_list_includes_accepting_signups(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
) -> None:
    """list 응답의 모든 매장에 accepting_signups 가 존재."""
    resp = await async_client.get("/api/v1/console/stores", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    stores = resp.json()
    assert len(stores) >= 1
    for store in stores:
        assert "accepting_signups" in store, store["name"]
        assert isinstance(store["accepting_signups"], bool)


async def test_patch_accepting_signups_false_reflected_in_get(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
    restore_accepting_signups,
) -> None:
    """PATCH false → detail/list 응답 모두 false 반영 (버그 회귀 방지)."""
    resp = await async_client.patch(
        f"/api/v1/console/stores/{test_store_id}/accepting-signups",
        headers=admin_headers,
        json={"accepting_signups": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepting_signups"] is False

    # detail 반영
    detail = await async_client.get(
        f"/api/v1/console/stores/{test_store_id}", headers=admin_headers
    )
    assert detail.json()["accepting_signups"] is False

    # list 반영
    listed = await async_client.get("/api/v1/console/stores", headers=admin_headers)
    target = next(s for s in listed.json() if s["id"] == str(test_store_id))
    assert target["accepting_signups"] is False


async def test_patch_accepting_signups_true_reenables(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
    restore_accepting_signups,
) -> None:
    """false 로 끈 뒤 PATCH true → 다시 true (재활성화 가능)."""
    await async_client.patch(
        f"/api/v1/console/stores/{test_store_id}/accepting-signups",
        headers=admin_headers,
        json={"accepting_signups": False},
    )

    resp = await async_client.patch(
        f"/api/v1/console/stores/{test_store_id}/accepting-signups",
        headers=admin_headers,
        json={"accepting_signups": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepting_signups"] is True

    detail = await async_client.get(
        f"/api/v1/console/stores/{test_store_id}", headers=admin_headers
    )
    assert detail.json()["accepting_signups"] is True
