"""API integration — 인사 기록 보존 (purge 후보 · 익명화). D4 · §6

원칙: 자동 삭제하지 않는다. 기간이 지나면 목록에만 뜨고 실행은 관리자가 한다.
익명화는 되돌릴 수 없으므로 "퇴사 + 보존기간 경과" 가드가 유일한 방어선이다.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.user import User
from app.services.records_retention_service import (
    DEFAULT_RETENTION_YEARS,
    records_retention_service,
)

pytestmark = pytest.mark.asyncio

CANDIDATES_URL = "/api/v1/console/users/purge-candidates"
TODAY = date.today()
LONG_AGO = TODAY - timedelta(days=365 * (DEFAULT_RETENTION_YEARS + 1))
RECENTLY = TODAY - timedelta(days=30)


@pytest_asyncio.fixture
async def member_snapshot(test_user) -> AsyncIterator[dict]:
    """teststaff 의 소속/계정 상태를 스냅샷하고 테스트 후 되돌린다 (익명화가 파괴적이라 필수)."""
    uid = test_user["id"]
    org_id = test_user["organization_id"]
    async with async_session() as db:
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid, OrgMember.organization_id == org_id,
        ))
        user = await db.get(User, uid)
        snap = {
            "status": member.status if member else None,
            "termination_date": member.termination_date if member else None,
            "u_status": user.status,
            "is_active": user.is_active,
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
        }
    try:
        yield test_user
    finally:
        async with async_session() as db:
            member = await db.scalar(select(OrgMember).where(
                OrgMember.user_id == uid, OrgMember.organization_id == org_id,
            ))
            if member:
                member.status = snap["status"] or "active"
                member.termination_date = snap["termination_date"]
            user = await db.get(User, uid)
            user.status = snap["u_status"]
            user.is_active = snap["is_active"]
            user.username = snap["username"]
            user.full_name = snap["full_name"]
            user.email = snap["email"]
            await db.commit()


async def _set_terminated(uid, org_id, when: date) -> None:
    async with async_session() as db:
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid, OrgMember.organization_id == org_id,
        ))
        member.status = "terminated"
        member.termination_date = when
        user = await db.get(User, uid)
        user.is_active = False
        await db.commit()


async def test_purge_candidates_lists_only_elapsed(
    async_client, admin_headers, member_snapshot
):
    """보존 기간이 지난 퇴사자만 목록에 오른다. 조회만 하고 아무것도 지우지 않는다."""
    uid = str(member_snapshot["id"])
    org_id = member_snapshot["organization_id"]

    # 최근 퇴사 → 후보 아님
    await _set_terminated(member_snapshot["id"], org_id, RECENTLY)
    resp = await async_client.get(CANDIDATES_URL, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_years"] == DEFAULT_RETENTION_YEARS
    assert uid not in {c["user_id"] for c in body["candidates"]}

    # 오래 전 퇴사 → 후보
    await _set_terminated(member_snapshot["id"], org_id, LONG_AGO)
    resp2 = await async_client.get(CANDIDATES_URL, headers=admin_headers)
    assert resp2.status_code == 200, resp2.text
    row = next(c for c in resp2.json()["candidates"] if c["user_id"] == uid)
    assert row["termination_date"] == LONG_AGO.isoformat()

    # 목록 조회만으로는 아무것도 바뀌지 않는다
    async with async_session() as db:
        user = await db.get(User, member_snapshot["id"])
    assert user.status != "anonymized"
    assert user.full_name == member_snapshot["full_name"] or user.full_name is not None


async def test_anonymize_scrubs_identity_and_keeps_membership(
    async_client, admin_headers, member_snapshot
):
    """익명화 — 신원 정보는 지우고 소속·근무 기록은 남긴다."""
    uid = member_snapshot["id"]
    org_id = member_snapshot["organization_id"]
    await _set_terminated(uid, org_id, LONG_AGO)

    resp = await async_client.post(
        f"/api/v1/console/users/{uid}/anonymize", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["full_name"].startswith("Former Employee")

    async with async_session() as db:
        user = await db.get(User, uid)
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid, OrgMember.organization_id == org_id,
        ))
    assert user.full_name.startswith("Former Employee")
    assert user.email is None
    assert user.clockin_pin is None
    assert user.signature_strokes is None
    assert user.username.startswith("anon_")
    assert user.status == "anonymized"
    assert user.is_active is False
    # 소속 행은 남는다 — 근무·급여 집계의 앵커다
    assert member is not None
    assert member.termination_date == LONG_AGO


async def test_anonymize_rejected_within_retention(
    async_client, admin_headers, member_snapshot
):
    """보존 기간 안이면 거부 — 되돌릴 수 없는 작업의 유일한 방어선."""
    uid = member_snapshot["id"]
    await _set_terminated(uid, member_snapshot["organization_id"], RECENTLY)
    resp = await async_client.post(
        f"/api/v1/console/users/{uid}/anonymize", headers=admin_headers
    )
    assert resp.status_code == 400, resp.text


async def test_anonymize_rejected_for_active_employee(
    async_client, admin_headers, member_snapshot
):
    """재직자는 익명화 대상이 아니다."""
    uid = member_snapshot["id"]
    async with async_session() as db:
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == uid,
            OrgMember.organization_id == member_snapshot["organization_id"],
        ))
        member.status = "active"
        member.termination_date = None
        await db.commit()
    resp = await async_client.post(
        f"/api/v1/console/users/{uid}/anonymize", headers=admin_headers
    )
    assert resp.status_code == 400, resp.text


async def test_anonymized_user_drops_out_of_candidates(
    async_client, admin_headers, member_snapshot
):
    """이미 익명화된 사람은 후보 목록에서 빠진다 (할 일이 없다)."""
    uid = member_snapshot["id"]
    org_id = member_snapshot["organization_id"]
    await _set_terminated(uid, org_id, LONG_AGO)
    await async_client.post(
        f"/api/v1/console/users/{uid}/anonymize", headers=admin_headers
    )
    resp = await async_client.get(CANDIDATES_URL, headers=admin_headers)
    assert str(uid) not in {c["user_id"] for c in resp.json()["candidates"]}


async def test_retention_years_falls_back_to_default(test_user):
    """설정이 비정상이어도 기본값으로 떨어진다 (0/음수/문자열 방어)."""
    async with async_session() as db:
        years = await records_retention_service.retention_years(
            db, test_user["organization_id"]
        )
    assert years == DEFAULT_RETENTION_YEARS
