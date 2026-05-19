"""Refresh token rotation + grace window 테스트.

회전된 refresh token 을 grace window 안에 재사용하면 캐시된 새 토큰을
그대로 반환하는지(멱등), grace 초과 시 차단되는지 검증한다.
멀티 탭/새로고침 race 로 인한 강제 로그아웃 회귀 방지가 목표.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.main import app
from app.models.token import RefreshToken
from app.models.user import User


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fresh_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_refresh_tokens():
    """각 테스트 전후 testadmin 의 refresh_tokens 를 비워 격리."""
    async def _purge():
        async with async_session() as db:
            row = (await db.execute(
                select(User).where(User.username == "testadmin")
            )).scalar_one_or_none()
            if row is None:
                return
            await db.execute(
                delete(RefreshToken).where(RefreshToken.user_id == row.id)
            )
            await db.commit()

    await _purge()
    yield
    await _purge()


async def _login(client: AsyncClient) -> tuple[str, str]:
    resp = await client.post(
        "/api/v1/console/auth/login",
        json={"username": "testadmin", "password": "1234"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["access_token"], body["refresh_token"]


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_returns_new_pair(fresh_client: AsyncClient):
    """정상 R1 → 새 access/refresh 쌍 발급 + R1 row 가 회전됨 상태로 전이."""
    _, refresh = await _login(fresh_client)
    resp = await fresh_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 새 refresh token 은 jti 가 다르므로 항상 다르다 (access 는 같은 초면 동일 가능).
    assert body["refresh_token"] != refresh
    # DB 상태: R1 은 살아있고 replaced_* 가 채워져 새 토큰을 가리킨다.
    async with async_session() as db:
        row = (await db.execute(
            select(RefreshToken).where(RefreshToken.token == refresh)
        )).scalar_one()
        assert row.replaced_at is not None
        assert row.replaced_by_token == body["refresh_token"]


@pytest.mark.asyncio
async def test_refresh_grace_returns_cached_pair(fresh_client: AsyncClient):
    """grace window 안 같은 R1 재요청 → 캐시된 새 쌍 그대로 반환 (멱등)."""
    _, refresh = await _login(fresh_client)
    r1 = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert r1.status_code == 200, r1.text
    pair1 = r1.json()

    r2 = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert r2.status_code == 200, r2.text
    pair2 = r2.json()

    assert pair2["access_token"] == pair1["access_token"]
    assert pair2["refresh_token"] == pair1["refresh_token"]


@pytest.mark.asyncio
async def test_refresh_grace_expired_rejects(fresh_client: AsyncClient):
    """grace 초과 후 같은 R1 재사용 → 401."""
    _, refresh = await _login(fresh_client)
    r1 = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert r1.status_code == 200, r1.text

    # replaced_at 을 grace + 여유 만큼 과거로 당겨 grace 초과 상황 시뮬레이션
    async with async_session() as db:
        row = (await db.execute(
            select(RefreshToken).where(RefreshToken.token == refresh)
        )).scalar_one()
        row.replaced_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.REFRESH_TOKEN_GRACE_SECONDS + 5
        )
        await db.commit()

    r2 = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_refresh_invalid_token_rejects(fresh_client: AsyncClient):
    """DB에 없는 임의 토큰 문자열 → 401."""
    r = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": "definitely-not-a-token"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_expired_token_rejects(fresh_client: AsyncClient):
    """expires_at < now → 401."""
    _, refresh = await _login(fresh_client)
    async with async_session() as db:
        row = (await db.execute(
            select(RefreshToken).where(RefreshToken.token == refresh)
        )).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    r = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_refresh_concurrent_race_idempotent(fresh_client: AsyncClient):
    """동시 5요청 모두 200 + 모두 같은 새 토큰 쌍 (race 멱등성 회귀 방지)."""
    _, refresh = await _login(fresh_client)
    coros = [
        fresh_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        for _ in range(5)
    ]
    results = await asyncio.gather(*coros)
    for r in results:
        assert r.status_code == 200, r.text
    bodies = [r.json() for r in results]
    first = bodies[0]
    for b in bodies[1:]:
        assert b["access_token"] == first["access_token"]
        assert b["refresh_token"] == first["refresh_token"]


@pytest.mark.asyncio
async def test_replaced_token_row_persists_with_cache(fresh_client: AsyncClient):
    """회전 후 R1 row 가 삭제되지 않고 replaced_* 필드가 모두 채워져 있다."""
    _, refresh = await _login(fresh_client)
    resp = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    assert resp.status_code == 200, resp.text
    new_pair = resp.json()

    async with async_session() as db:
        row = (await db.execute(
            select(RefreshToken).where(RefreshToken.token == refresh)
        )).scalar_one_or_none()
        assert row is not None, "R1 row should not be deleted on rotation"
        assert row.replaced_at is not None
        assert row.replaced_by_token == new_pair["refresh_token"]
        assert row.replaced_access_token == new_pair["access_token"]


@pytest.mark.asyncio
async def test_new_refresh_token_works(fresh_client: AsyncClient):
    """회전 후 받은 새 R2 로 다시 refresh → R3 발급 정상."""
    _, refresh = await _login(fresh_client)
    r1 = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh}
    )
    new_refresh = r1.json()["refresh_token"]

    r2 = await fresh_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_refresh}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["refresh_token"] != new_refresh
