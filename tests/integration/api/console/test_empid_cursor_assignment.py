"""API integration tests — 매장 배정 자동 채번이 커서에서 나오는지 (트랙 S2 / RULE-A).

계약 SoT: `docs/99_inbox/2026-08-18 empid 채번 API계약·규칙.md` §2 RULE-A.

커서 API(§3-1~3-3)는 test_store_numbering.py(S3) 가 본다. 여기서 보는 것은
**배정 경로**다 — POST /users/{id}/stores/{store_id} 가 org_numbering 게이트웨이를
타고 커서에서 번호를 받아 커서를 전진시키는가, 그리고 예외 번호가 섞여도
다음 사람이 그리로 끌려가지 않는가(커서를 도입한 이유).

매장은 테스트마다 새로 만들고 끝나면 하드 삭제한다. 매장 생성은 API 가 아니라
DB 직접 — 콘솔 생성 경로는 Owner 자동 배정으로 커서를 먼저 움직여 절대값 가정이 깨진다.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.org_member import (
    EMPID_KIND_EXCEPTION,
    EMPID_KIND_SEQUENCE,
    OrgMember,
    OrgMemberStore,
)
from app.models.organization import Store
from app.models.user import User

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/console"


@pytest_asyncio.fixture
async def cursor_store(seed_organization: dict) -> AsyncIterator[UUID]:
    """커서 1000 으로 시작하는 빈 매장 — 종료 시 하드 삭제(배정 행은 CASCADE)."""
    async with async_session() as s:
        store = Store(
            organization_id=seed_organization["id"],
            name=f"__empidcur_{uuid4().hex[:8]}__",
            timezone="UTC",
            next_empid=1000,
        )
        s.add(store)
        await s.commit()
        store_id = store.id
    try:
        yield store_id
    finally:
        async with async_session() as s:
            await s.execute(delete(Store).where(Store.id == store_id))
            await s.commit()


async def _cursor(store_id: UUID) -> int | None:
    async with async_session() as s:
        return (
            await s.execute(select(Store.next_empid).where(Store.id == store_id))
        ).scalar_one()


async def _empid(user_id: UUID, store_id: UUID) -> int | None:
    async with async_session() as s:
        return (
            await s.execute(
                select(OrgMemberStore.empid)
                .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
                .where(OrgMember.user_id == user_id, OrgMemberStore.store_id == store_id)
            )
        ).scalar_one_or_none()


async def _write_exception_empid(
    seed_organization: dict, store_id: UUID, empid: int
) -> None:
    """대역 밖 예외 번호를 가진 인원을 매장에 심는다 (본사 이관 인원 흉내)."""
    async with async_session() as s:
        member_id = (
            await s.execute(
                select(OrgMember.id)
                .where(OrgMember.organization_id == seed_organization["id"])
                .limit(1)
            )
        ).scalar_one()
        s.add(OrgMemberStore(
            org_member_id=member_id, store_id=store_id,
            empid=empid, empid_kind=EMPID_KIND_EXCEPTION,
        ))
        await s.commit()


# ---------------------------------------------------------------------------
# RULE-A — 배정이 커서에서 발급하고 커서를 전진시킨다
# ---------------------------------------------------------------------------


async def test_assign_store_issues_from_cursor_and_advances(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    cursor_store: UUID,
) -> None:
    staff = test_users["teststaff"]
    resp = await async_client.post(
        f"{BASE}/users/{staff['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    assert await _empid(staff["id"], cursor_store) == 1000
    assert await _cursor(cursor_store) == 1001

    supervisor = test_users["testsv"]
    resp = await async_client.post(
        f"{BASE}/users/{supervisor['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    assert await _empid(supervisor["id"], cursor_store) == 1001
    assert await _cursor(cursor_store) == 1002


async def test_assign_store_is_not_dragged_by_exception_empid(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    seed_organization: dict,
    cursor_store: UUID,
) -> None:
    """예외 번호 90001 이 매장에 있어도 다음 배정은 커서(1000). 구 MAX+1 이면 90002 였다."""
    await _write_exception_empid(seed_organization, cursor_store, 90001)

    staff = test_users["teststaff"]
    resp = await async_client.post(
        f"{BASE}/users/{staff['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    assert await _empid(staff["id"], cursor_store) == 1000
    assert await _cursor(cursor_store) == 1001


async def test_assign_store_skips_taken_number_and_corrects_cursor(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    seed_organization: dict,
    cursor_store: UUID,
) -> None:
    """커서가 이미 쓰인 번호를 가리키면 건너뛰고 발급 + 커서 정정 (INV-3)."""
    async with async_session() as s:
        member_id = (
            await s.execute(
                select(OrgMember.id)
                .join(User, User.id == OrgMember.user_id)
                .where(User.username == "testgm")
            )
        ).scalar_one()
        s.add(OrgMemberStore(
            org_member_id=member_id, store_id=cursor_store,
            empid=1000, empid_kind=EMPID_KIND_SEQUENCE,
        ))
        await s.commit()

    staff = test_users["teststaff"]
    resp = await async_client.post(
        f"{BASE}/users/{staff['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    assert await _empid(staff["id"], cursor_store) == 1001
    assert await _cursor(cursor_store) == 1002


async def test_unassign_store_leaves_cursor_untouched(
    async_client: AsyncClient,
    admin_headers: dict,
    test_users: dict,
    cursor_store: UUID,
) -> None:
    """배정 해제는 번호를 반납하지도, 커서를 되돌리지도 않는다 (INV-4)."""
    staff = test_users["teststaff"]
    await async_client.post(
        f"{BASE}/users/{staff['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert await _cursor(cursor_store) == 1001

    resp = await async_client.delete(
        f"{BASE}/users/{staff['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert resp.status_code == 204, resp.text
    assert await _cursor(cursor_store) == 1001
    assert await _empid(staff["id"], cursor_store) == 1000  # 번호는 휴면 행에 남는다

    # 재배정 — 같은 행 재사용이라 커서는 그대로
    resp = await async_client.post(
        f"{BASE}/users/{staff['id']}/stores/{cursor_store}", headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    assert await _empid(staff["id"], cursor_store) == 1000
    assert await _cursor(cursor_store) == 1001
