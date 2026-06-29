"""API integration tests — store Phase 1 신규 기능.

대상 (이번 phase 신규/수정):
    - status enum (preparing/open/paused/closed) — is_active 파생 대체
    - phone / email 신규 필드 수집·반영
    - status=closed → soft-delete(목록 제외), 재활성화 복구
    - PUT /console/stores/reorder — 드래그 정렬 일괄 변경

기존 is_active(bool) 컬럼은 제거되고 status 가 SoT. 응답의 is_active 는 status==open 파생.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/console/stores"


async def _create_store(client, headers, **kwargs):
    payload = {"name": kwargs.pop("name", "Phase1 Test Store")}
    payload.update(kwargs)
    return await client.post(BASE, headers=headers, json=payload)


# ── 생성: status 기본값 + 신규 필드 ───────────────────────────

async def test_create_store_defaults_status_open(
    async_client: AsyncClient, admin_headers: dict
):
    """status 미지정 생성 → 기본 open, is_active 파생 true."""
    store_id = None
    try:
        resp = await _create_store(async_client, admin_headers, name="P1 Default Status")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        store_id = body["id"]
        assert body["status"] == "open"
        assert body["is_active"] is True
        assert "sort_order" in body
    finally:
        if store_id:
            await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


async def test_create_store_with_preparing_and_contacts(
    async_client: AsyncClient, admin_headers: dict
):
    """생성 시 status=preparing + phone/email 수집·반영."""
    store_id = None
    try:
        resp = await _create_store(
            async_client, admin_headers,
            name="P1 Preparing Store", status="preparing",
            phone="010-1234-5678", email="branch@example.com",
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        store_id = body["id"]
        assert body["status"] == "preparing"
        assert body["is_active"] is False  # preparing != open
        assert body["phone"] == "010-1234-5678"
        assert body["email"] == "branch@example.com"
    finally:
        if store_id:
            await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


async def test_create_store_invalid_status_422(
    async_client: AsyncClient, admin_headers: dict
):
    """허용되지 않은 status → 422."""
    resp = await _create_store(async_client, admin_headers, name="P1 Bad Status", status="banana")
    assert resp.status_code == 422, resp.text


# ── 수정: status 전이 + is_active 파생 ────────────────────────

async def test_update_status_paused_derives_inactive(
    async_client: AsyncClient, admin_headers: dict
):
    """status=paused → is_active 파생 false, 목록엔 여전히 노출(폐점 아님)."""
    store_id = None
    try:
        created = await _create_store(async_client, admin_headers, name="P1 Pause Me")
        store_id = created.json()["id"]
        resp = await async_client.put(
            f"{BASE}/{store_id}", headers=admin_headers, json={"status": "paused"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "paused"
        assert resp.json()["is_active"] is False
        # paused 는 soft-delete 아님 → 목록 유지
        listed = await async_client.get(BASE, headers=admin_headers)
        assert any(s["id"] == store_id for s in listed.json())
    finally:
        if store_id:
            await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


async def test_update_status_closed_soft_deletes_then_reactivate(
    async_client: AsyncClient, admin_headers: dict
):
    """status=closed → 목록 제외(soft-delete). 다시 open → 복구."""
    store_id = None
    try:
        created = await _create_store(async_client, admin_headers, name="P1 Close Me")
        store_id = created.json()["id"]

        # 폐점
        closed = await async_client.put(
            f"{BASE}/{store_id}", headers=admin_headers, json={"status": "closed"}
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "closed"
        listed = await async_client.get(BASE, headers=admin_headers)
        assert not any(s["id"] == store_id for s in listed.json()), "closed 매장은 목록 제외"

        # 재오픈 → deleted_at 해제, 목록 복귀
        reopened = await async_client.put(
            f"{BASE}/{store_id}", headers=admin_headers, json={"status": "open"}
        )
        assert reopened.status_code == 200, reopened.text
        listed2 = await async_client.get(BASE, headers=admin_headers)
        assert any(s["id"] == store_id for s in listed2.json()), "재오픈 매장은 목록 복귀"
    finally:
        if store_id:
            await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


# ── code 자동생성 + 충돌 넘버링 ──────────────────────────────

async def test_create_store_auto_generates_code_from_name(
    async_client: AsyncClient, admin_headers: dict
):
    """code 미지정 → 이름 앞 3글자(영숫자) 대문자로 자동 생성."""
    store_id = None
    try:
        resp = await _create_store(async_client, admin_headers, name="Phoenix Roastery")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        store_id = body["id"]
        assert body["code"] == "PHO", body["code"]
    finally:
        if store_id:
            await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


async def test_create_store_auto_code_collision_appends_number(
    async_client: AsyncClient, admin_headers: dict
):
    """같은 앞3글자 매장이 이미 있으면 2,3… 접미사로 충돌 회피."""
    ids = []
    try:
        first = await _create_store(async_client, admin_headers, name="Zeta One")
        assert first.json()["code"] == "ZET", first.text
        ids.append(first.json()["id"])

        second = await _create_store(async_client, admin_headers, name="Zeta Two")
        assert second.json()["code"] == "ZET2", second.text
        ids.append(second.json()["id"])

        third = await _create_store(async_client, admin_headers, name="Zeta Three")
        assert third.json()["code"] == "ZET3", third.text
        ids.append(third.json()["id"])
    finally:
        for sid in ids:
            await async_client.delete(f"{BASE}/{sid}", headers=admin_headers)


async def test_update_store_code_duplicate_returns_409(
    async_client: AsyncClient, admin_headers: dict
):
    """수정 시 org 내 기존 코드와 충돌 → 409 (org-scoped dedupe)."""
    ids = []
    try:
        a = await _create_store(async_client, admin_headers, name="Dedupe A", code="AAA")
        b = await _create_store(async_client, admin_headers, name="Dedupe B", code="BBB")
        ids = [a.json()["id"], b.json()["id"]]
        # B 를 A 의 코드로 변경 시도 → 409
        resp = await async_client.put(
            f"{BASE}/{ids[1]}", headers=admin_headers, json={"code": "AAA"}
        )
        assert resp.status_code == 409, resp.text
    finally:
        for sid in ids:
            await async_client.delete(f"{BASE}/{sid}", headers=admin_headers)


async def test_include_closed_reveals_closed_stores_for_recovery(
    async_client: AsyncClient, admin_headers: dict
):
    """include_closed=true 면 폐점 매장이 목록에 보임 (복구 화면용). 기본은 숨김."""
    store_id = None
    try:
        created = await _create_store(async_client, admin_headers, name="Recover Me")
        store_id = created.json()["id"]
        await async_client.put(
            f"{BASE}/{store_id}", headers=admin_headers, json={"status": "closed"}
        )
        # 기본 목록엔 없음
        default_list = await async_client.get(BASE, headers=admin_headers)
        assert not any(s["id"] == store_id for s in default_list.json())
        # include_closed 면 보임 + status=closed
        with_closed = await async_client.get(
            BASE, headers=admin_headers, params={"include_closed": "true"}
        )
        match = next((s for s in with_closed.json() if s["id"] == store_id), None)
        assert match is not None, "include_closed=true 에서 폐점 매장이 보여야 함"
        assert match["status"] == "closed"
    finally:
        if store_id:
            await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


# ── closed 매장 새 생성 차단 (라이프사이클 게이트) ──────────────

async def test_assert_open_for_create_blocks_closed_store(
    async_client: AsyncClient, admin_headers: dict
):
    """store_service.assert_open_for_create — open 통과, closed(폐점) 시 ConflictError(409)."""
    from app.database import async_session
    from app.services.store_service import store_service
    from app.utils.exceptions import ConflictError
    from uuid import UUID

    created = await _create_store(async_client, admin_headers, name="Gate Service Store")
    store_id = UUID(created.json()["id"])
    try:
        async with async_session() as db:
            await store_service.assert_open_for_create(db, store_id)  # open → 통과

        await async_client.put(
            f"{BASE}/{store_id}", headers=admin_headers, json={"status": "closed"}
        )
        async with async_session() as db:
            with pytest.raises(ConflictError):
                await store_service.assert_open_for_create(db, store_id)
    finally:
        await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


async def test_closed_store_blocks_schedule_creation_409(
    async_client: AsyncClient, admin_headers: dict, test_users: dict
):
    """폐점 매장에 새 스케줄 생성 시 409 (closed 게이트가 create_entry 진입부에서 차단)."""
    created = await _create_store(async_client, admin_headers, name="Gate Schedule Store")
    store_id = created.json()["id"]
    try:
        await async_client.put(
            f"{BASE}/{store_id}", headers=admin_headers, json={"status": "closed"}
        )
        resp = await async_client.post(
            "/api/v1/console/schedules",
            headers=admin_headers,
            json={
                "store_id": store_id,
                "user_id": str(test_users["teststaff"]["id"]),
                "work_date": str(date.today()),
                "start_time": "09:00",
                "end_time": "17:00",
                "status": "confirmed",
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].get("code") == "store_closed", resp.text
    finally:
        await async_client.delete(f"{BASE}/{store_id}", headers=admin_headers)


# ── reorder ──────────────────────────────────────────────────

async def test_reorder_stores_changes_sort_order(
    async_client: AsyncClient, admin_headers: dict
):
    """현재 목록 순서를 역순으로 reorder → list 가 새 순서를 반영."""
    a_id = b_id = None
    try:
        a = await _create_store(async_client, admin_headers, name="P1 Reorder A")
        b = await _create_store(async_client, admin_headers, name="P1 Reorder B")
        a_id, b_id = a.json()["id"], b.json()["id"]

        current = [s["id"] for s in (await async_client.get(BASE, headers=admin_headers)).json()]
        reversed_ids = list(reversed(current))

        resp = await async_client.put(
            f"{BASE}/reorder", headers=admin_headers, json={"store_ids": reversed_ids}
        )
        assert resp.status_code == 204, resp.text

        after = [s["id"] for s in (await async_client.get(BASE, headers=admin_headers)).json()]
        assert after == reversed_ids, "reorder 후 목록 순서가 요청 순서와 일치해야 함"
    finally:
        for sid in (a_id, b_id):
            if sid:
                await async_client.delete(f"{BASE}/{sid}", headers=admin_headers)
