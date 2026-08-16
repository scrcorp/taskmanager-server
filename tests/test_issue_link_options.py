"""API integration — 이슈 작성 화면의 link-options.

회귀 방지 대상: users[] 에 **role_priority 가 반드시 들어간다.**

앱/콘솔의 "Related role" 칩(Owner/GM/SV/Staff/All)은 이 값으로 각 직급의 인원수를
세고, count == 0 이면 칩을 비활성화한다. 서버가 role_priority 를 빼먹으면 네 칩이
전부 count 0 이 되어 **All 칩 하나만 눌리는** 상태가 된다 (실제로 그랬다).
role_name 만으로는 대체할 수 없다 — 커스텀 role 이름은 자유 문자열이다.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.permissions import (
    GM_PRIORITY,
    STAFF_PRIORITY,
    SV_PRIORITY,
)
from app.database import async_session
from app.main import app
from app.models.user import User as UserModel
from app.models.user_store import UserStore


async def _login(username: str) -> str:
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(UserModel).where(UserModel.username == username))
        ).scalar_one()
        return create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def store_members(seed_organization: dict, test_store_id: UUID):
    """testgm/testsv/teststaff 를 대상 매장에 배정(idempotent) + 사후 원복.

    link-options 는 "그 매장에 배정된 사람" 만 응답하고, 배정 안 된 요청자는 403 이다.
    """
    org_id: UUID = seed_organization["id"]
    usernames = ["testgm", "testsv", "teststaff"]
    created: list[tuple[UUID, UUID]] = []

    async with async_session() as db:
        users = {
            u.username: u
            for u in (
                await db.execute(
                    select(UserModel).where(
                        UserModel.username.in_(usernames),
                        UserModel.organization_id == org_id,
                    )
                )
            )
            .scalars()
            .all()
        }
        for name in usernames:
            u = users.get(name)
            if u is None:
                continue
            link = (
                await db.execute(
                    select(UserStore).where(
                        UserStore.user_id == u.id,
                        UserStore.store_id == test_store_id,
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                db.add(
                    UserStore(
                        user_id=u.id,
                        store_id=test_store_id,
                        is_manager=(name != "teststaff"),
                    )
                )
                created.append((u.id, test_store_id))
        await db.commit()

    yield {name: u.id for name, u in users.items()}

    async with async_session() as db:
        for uid, sid in created:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == sid
                )
            )
        await db.commit()


async def _fetch_users(client: AsyncClient, token: str, store_id: UUID) -> list[dict]:
    resp = await client.get(
        f"/api/v1/app/my/stores/{store_id}/link-options", headers=_h(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["users"]


@pytest.mark.asyncio
async def test_users_carry_role_priority(client, store_members, test_store_id):
    """모든 user 항목에 role_priority 키가 있고, 값이 실제 직급과 맞아야 한다."""
    token = await _login("testsv")
    users = await _fetch_users(client, token, test_store_id)
    assert users, "매장에 유저가 있어야 의미 있는 검증이 된다"

    for u in users:
        assert "role_priority" in u, f"role_priority 누락: {u}"

    by_name = {u["username"]: u for u in users}
    expected = {
        "testgm": GM_PRIORITY,
        "testsv": SV_PRIORITY,
        "teststaff": STAFF_PRIORITY,
    }
    for username, priority in expected.items():
        if username in by_name:
            assert by_name[username]["role_priority"] == priority, (
                f"{username} 의 role_priority 가 {priority} 여야 한다"
            )


@pytest.mark.asyncio
async def test_role_chip_counts_are_nonzero(client, store_members, test_store_id):
    """클라의 칩 count 계산을 그대로 재현 — All 말고도 최소 하나는 활성이어야 한다.

    앱 `issue_report_link_picker.dart` 의 _matchOwner/_matchGm/... 와 동일한 식이다.
    """
    token = await _login("testsv")
    users = await _fetch_users(client, token, test_store_id)

    counts = {
        "gm": sum(1 for u in users if u.get("role_priority") == GM_PRIORITY),
        "sv": sum(1 for u in users if u.get("role_priority") == SV_PRIORITY),
        "staff": sum(1 for u in users if u.get("role_priority") == STAFF_PRIORITY),
    }
    assert sum(counts.values()) > 0, (
        f"직급 칩이 전부 0 이면 All 만 눌리는 그 버그다: {counts}"
    )
    assert counts["sv"] > 0, "요청자(testsv) 본인이 매장에 있으므로 SV 는 0 일 수 없다"


@pytest.mark.asyncio
async def test_role_name_still_present(client, store_members, test_store_id):
    """role_priority 추가가 기존 role_name 계약을 깨지 않았는지 (additive 확인)."""
    token = await _login("testsv")
    users = await _fetch_users(client, token, test_store_id)
    for u in users:
        assert "role_name" in u
        assert {"id", "username", "full_name"} <= set(u)
