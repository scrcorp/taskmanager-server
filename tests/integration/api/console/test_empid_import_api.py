"""API integration — EMPID 임포트 확장 (계약 §3-4 / §3-5 / §3-6).

commit 은 **임포트 탭 · bulk 에디터 · 스태프 상세 3화면 공용**이다. 여기서는
- 기존 필드/동작이 그대로인지 (empid_kind 를 안 보내는 구버전 요청)
- empid_kind / reason 이 반영되고 exception_count · cursor_after 가 돌아오는지
- preview 응답의 distribution, roster 항목의 empid_kind
를 HTTP 계약 그대로 확인한다.
"""
from __future__ import annotations

import io
from typing import AsyncIterator

import openpyxl
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/console/empid-import"


async def _member_id(db, user_id, org_id):
    return (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.user_id == user_id, OrgMember.organization_id == org_id
            )
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def empid_target(test_user, test_store_id) -> AsyncIterator[dict]:
    """teststaff 를 매장에 배정(번호 없이)하고 매장 커서를 고정. 종료 시 원상 복구."""
    uid = test_user["id"]
    org_id = test_user["organization_id"]
    async with async_session() as db:
        member_id = await _member_id(db, uid, org_id)
        row = await db.scalar(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == member_id,
                OrgMemberStore.store_id == test_store_id,
            )
        )
        created = row is None
        before = None if created else (row.empid, row.empid_kind)
        if created:
            db.add(OrgMemberStore(
                org_member_id=member_id, store_id=test_store_id,
                is_manager=False, is_work_assignment=True, empid=None,
            ))
        else:
            row.empid = None
            row.empid_kind = "sequence"
        store = await db.get(Store, test_store_id)
        cursor_before = store.next_empid
        store.next_empid = 500  # 백필 상태를 흉내 — 판정 기준을 고정한다
        await db.commit()

    yield {"user_id": str(uid), "store_id": str(test_store_id), "org_id": org_id}

    async with async_session() as db:
        row = await db.scalar(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == member_id,
                OrgMemberStore.store_id == test_store_id,
            )
        )
        if created:
            if row is not None:
                await db.delete(row)
        elif row is not None:
            row.empid, row.empid_kind = before
        store = await db.get(Store, test_store_id)
        store.next_empid = cursor_before
        await db.commit()


async def _kind_and_empid(org_id, user_id, store_id) -> tuple[int | None, str]:
    async with async_session() as db:
        member_id = await _member_id(db, user_id, org_id)
        row = (
            await db.execute(
                select(OrgMemberStore.empid, OrgMemberStore.empid_kind).where(
                    OrgMemberStore.org_member_id == member_id,
                    OrgMemberStore.store_id == store_id,
                )
            )
        ).one()
        return row.empid, row.empid_kind


async def test_commit_without_kind_keeps_old_contract(
    async_client: AsyncClient, admin_headers: dict, empid_target: dict
) -> None:
    """구버전 요청(empid_kind 없음) — 기존 필드 그대로 + 기본값 sequence (INV-6)."""
    resp = await async_client.post(
        f"{BASE}/commit",
        headers=admin_headers,
        json={"assignments": [{
            "user_id": empid_target["user_id"],
            "store_id": empid_target["store_id"],
            "empid": 501,
        }]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] and body["applied"][0]["empid"] == 501
    assert body["renumbered"] == [] and body["skipped"] == [] and body["rejected"] == []
    assert body["exception_count"] == 0
    # 수동 기입은 커서를 밀지 않는다 (INV-5) — 커밋 후 커서를 그대로 알려준다
    assert body["cursor_after"] == {empid_target["store_id"]: 500}

    empid, kind = await _kind_and_empid(
        empid_target["org_id"], empid_target["user_id"], empid_target["store_id"]
    )
    assert (empid, kind) == (501, "sequence")


async def test_commit_with_exception_kind_and_reason(
    async_client: AsyncClient, admin_headers: dict, empid_target: dict
) -> None:
    resp = await async_client.post(
        f"{BASE}/commit",
        headers=admin_headers,
        json={"assignments": [{
            "user_id": empid_target["user_id"],
            "store_id": empid_target["store_id"],
            "empid": 6012,
            "empid_kind": "exception",
            "reason": "transferred from HQ",
        }]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exception_count"] == 1
    assert body["cursor_after"] == {empid_target["store_id"]: 500}

    empid, kind = await _kind_and_empid(
        empid_target["org_id"], empid_target["user_id"], empid_target["store_id"]
    )
    assert (empid, kind) == (6012, "exception")


async def test_commit_rejects_unknown_kind_value(
    async_client: AsyncClient, admin_headers: dict, empid_target: dict
) -> None:
    """허용값 밖의 구분은 422 — 조용히 sequence 로 떨어뜨리지 않는다."""
    resp = await async_client.post(
        f"{BASE}/commit",
        headers=admin_headers,
        json={"assignments": [{
            "user_id": empid_target["user_id"],
            "store_id": empid_target["store_id"],
            "empid": 502,
            "empid_kind": "legacy",
        }]},
    )
    assert resp.status_code == 422


async def test_roster_items_carry_empid_kind(
    async_client: AsyncClient, admin_headers: dict, empid_target: dict
) -> None:
    await async_client.post(
        f"{BASE}/commit",
        headers=admin_headers,
        json={"assignments": [{
            "user_id": empid_target["user_id"],
            "store_id": empid_target["store_id"],
            "empid": 6013,
            "empid_kind": "exception",
        }]},
    )
    resp = await async_client.get(f"{BASE}/roster", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    store_row = next(
        s for s in resp.json() if s["store_id"] == empid_target["store_id"]
    )
    member = next(
        m for m in store_row["members"] if m["user_id"] == empid_target["user_id"]
    )
    assert member["empid_kind"] == "exception"


async def test_roster_items_carry_is_active(
    async_client: AsyncClient, admin_headers: dict, empid_target: dict
) -> None:
    """roster 는 계정 활성 여부를 싣는다 — 콘솔 export 의 비활성 제외 필터 축."""
    from uuid import UUID

    from app.models.user import User

    uid = UUID(empid_target["user_id"])

    async def _read_member() -> dict:
        resp = await async_client.get(f"{BASE}/roster", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        store_row = next(
            s for s in resp.json() if s["store_id"] == empid_target["store_id"]
        )
        return next(
            m for m in store_row["members"] if m["user_id"] == empid_target["user_id"]
        )

    member = await _read_member()
    assert member["is_active"] is True

    async with async_session() as db:
        user = await db.get(User, uid)
        user.is_active = False
        await db.commit()
    try:
        member = await _read_member()
        assert member["is_active"] is False
    finally:
        async with async_session() as db:
            user = await db.get(User, uid)
            user.is_active = True
            await db.commit()


async def test_preview_returns_hundred_band_distribution(
    async_client: AsyncClient, admin_headers: dict, empid_target: dict
) -> None:
    """업로드 파일의 번호 분포 — 100 단위, export split_by="band" 와 같은 규칙.

    분포는 사람 매칭 여부와 무관하게 파일의 유효 번호를 센다(어느 대역을 쓰는가의 그림).
    """
    async with async_session() as db:
        store = await db.get(Store, empid_target["store_id"])
        store_name = store.name

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["COMPANY", "CORP_ABR_3", "Name", "emp_id", "Email"])
    ws.append([store_name, "", "Ghost One", "1001", "ghost.one@example.com"])
    ws.append([store_name, "", "Ghost Two", "1002", "ghost.two@example.com"])
    ws.append([store_name, "", "Ghost Three", "6012", "ghost.three@example.com"])
    ws.append([store_name, "", "Ghost Bad", "abc", "ghost.bad@example.com"])
    buf = io.BytesIO()
    wb.save(buf)

    resp = await async_client.post(
        f"{BASE}/preview",
        headers=admin_headers,
        files={"file": ("roster.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    dist = resp.json()["distribution"]
    assert dist == [
        {"band": "1000-1099", "lo": 1000, "hi": 1099, "count": 2},
        {"band": "6000-6099", "lo": 6000, "hi": 6099, "count": 1},
    ]
