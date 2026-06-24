"""Control plane (운영자 평면) P1 테스트 — 인증/세션/보안.

org 권한과 독립된 평면이라 DB seed가 필요 없다. async_client(ASGITransport)로
실제 네트워크 없이 앱을 직접 호출한다. 활성화/계정은 worktree .env 값 사용
(CONTROL_PLANE_PATH=_cp_local_dev, user=ops, pw=control1234).
"""

import pytest
from httpx import AsyncClient

from app.api.control import ratelimit
from app.api.control.deps import COOKIE_NAME
from app.config import settings

pytestmark = pytest.mark.asyncio

BASE = "/" + settings.CONTROL_PLANE_PATH.strip("/")
USER = settings.CONTROL_ADMIN_USERNAME
PW = "control1234"  # worktree .env 해시의 평문


async def _login(client: AsyncClient, username: str = USER, password: str = PW):
    return await client.post(
        f"{BASE}/login",
        data={"username": username, "password": password},
    )


async def test_enabled_in_test_env() -> None:
    """전제 — 테스트 .env에서 control plane이 활성이어야 한다."""
    assert settings.control_plane_enabled is True


async def test_secret_path_404(async_client: AsyncClient) -> None:
    """비밀경로가 아니면 마운트 안 됨 → 404."""
    resp = await async_client.get("/not-the-secret-path/login")
    assert resp.status_code == 404


async def test_login_page_renders(async_client: AsyncClient) -> None:
    resp = await async_client.get(f"{BASE}/login")
    assert resp.status_code == 200
    assert "Operator Login" in resp.text
    # 검색엔진 비색인 헤더
    assert "noindex" in resp.headers.get("x-robots-tag", "")


async def test_dashboard_requires_auth(async_client: AsyncClient) -> None:
    """미인증 대시보드 접근 → 로그인으로 redirect."""
    resp = await async_client.get(f"{BASE}/dashboard")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{BASE}/login"


async def test_login_wrong_credentials(async_client: AsyncClient) -> None:
    resp = await _login(async_client, password="wrong-password")
    assert resp.status_code == 401
    assert "Invalid credentials" in resp.text
    assert COOKIE_NAME not in resp.cookies


async def test_login_success_sets_cookie_and_dashboard(async_client: AsyncClient) -> None:
    resp = await _login(async_client)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{BASE}/dashboard"
    assert COOKIE_NAME in resp.cookies  # 세션 쿠키 발급

    # 같은 클라이언트(쿠키 보유)로 대시보드 접근 → 200
    dash = await async_client.get(f"{BASE}/dashboard")
    assert dash.status_code == 200
    assert "Dashboard" in dash.text
    assert f"as <b>{USER}</b>" in dash.text


async def test_logout_clears_cookie(async_client: AsyncClient) -> None:
    await _login(async_client)
    resp = await async_client.post(f"{BASE}/logout")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{BASE}/login"
    # 로그아웃 후 대시보드 → 다시 로그인으로
    dash = await async_client.get(f"{BASE}/dashboard")
    assert dash.status_code == 303


async def test_tampered_cookie_rejected(async_client: AsyncClient) -> None:
    """서명 위조 쿠키 → 미인증 취급."""
    async_client.cookies.set(COOKIE_NAME, "forged.payload", path=BASE)
    resp = await async_client.get(f"{BASE}/dashboard")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{BASE}/login"


async def test_rate_limit_locks_after_max_fails(async_client: AsyncClient) -> None:
    """연속 실패 MAX_FAILS회 → 잠금(429)."""
    ratelimit._FAILS.clear()  # 다른 테스트 오염 제거
    for _ in range(ratelimit.MAX_FAILS):
        r = await _login(async_client, password="nope")
        assert r.status_code == 401
    locked = await _login(async_client, password="nope")
    assert locked.status_code == 429
    assert "Too many attempts" in locked.text
    # 올바른 자격증명이어도 잠금 동안 차단
    still = await _login(async_client)
    assert still.status_code == 429
    ratelimit._FAILS.clear()
