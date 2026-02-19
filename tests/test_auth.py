"""인증 API 테스트 — 로그인, 토큰 갱신, 로그아웃, /me 엔드포인트.

Auth API tests — Login, token refresh, logout, and /me endpoints.
Covers both admin and app authentication flows with strict edge case testing.
"""

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header

ADMIN_AUTH = "/api/v1/admin/auth"
APP_AUTH = "/api/v1/app/auth"


# ===== Admin Login =====

class TestAdminLogin:
    """관리자 로그인 테스트."""

    async def test_admin_login_success(self, client: AsyncClient, admin_user, org):
        """관리자 로그인 성공."""
        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
            "company_code": org.code,
        })
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_admin_login_without_company_code(self, client: AsyncClient, admin_user):
        """회사 코드 없이 로그인 (조직이 1개일 때)."""
        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
        })
        assert res.status_code == 200

    async def test_admin_login_wrong_password(self, client: AsyncClient, admin_user):
        """잘못된 비밀번호로 로그인 실패."""
        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "wrong_password",
        })
        assert res.status_code == 401

    async def test_admin_login_nonexistent_user(self, client: AsyncClient, org, roles):
        """존재하지 않는 사용자로 로그인 실패."""
        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "nonexistent",
            "password": "whatever",
        })
        assert res.status_code == 401

    async def test_admin_login_staff_rejected(self, client: AsyncClient, staff_user):
        """스태프 계정으로 관리자 로그인 시 403."""
        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "staff",
            "password": "staff123!",
        })
        assert res.status_code == 403

    async def test_admin_login_inactive_user(self, client: AsyncClient, db, admin_user):
        """비활성 계정 로그인 실패."""
        admin_user.is_active = False
        await db.flush()

        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
        })
        assert res.status_code == 401

    async def test_admin_login_invalid_company_code(self, client: AsyncClient, admin_user):
        """잘못된 회사 코드로 로그인 실패."""
        res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
            "company_code": "ZZZZZZ",
        })
        assert res.status_code == 404


# ===== Token Refresh =====

class TestTokenRefresh:
    """토큰 갱신 테스트."""

    async def test_refresh_token_success(self, client: AsyncClient, admin_user, org):
        """리프레시 토큰으로 새 토큰 발급."""
        # 먼저 로그인
        login_res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
        })
        refresh_token = login_res.json()["refresh_token"]

        # 토큰 갱신
        res = await client.post(f"{ADMIN_AUTH}/refresh", json={
            "refresh_token": refresh_token,
        })
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_refresh_with_invalid_token(self, client: AsyncClient, org, roles):
        """유효하지 않은 리프레시 토큰으로 갱신 실패."""
        res = await client.post(f"{ADMIN_AUTH}/refresh", json={
            "refresh_token": "invalid.token.here",
        })
        assert res.status_code == 401

    async def test_refresh_new_token_works(self, client: AsyncClient, admin_user, org):
        """갱신된 토큰으로 API 접근 가능."""
        login_res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
        })
        old_refresh = login_res.json()["refresh_token"]

        res = await client.post(f"{ADMIN_AUTH}/refresh", json={
            "refresh_token": old_refresh,
        })
        assert res.status_code == 200
        new_access = res.json()["access_token"]

        # 새 액세스 토큰으로 /me 접근 가능
        me_res = await client.get(f"{ADMIN_AUTH}/me", headers=auth_header(new_access))
        assert me_res.status_code == 200
        assert me_res.json()["username"] == "admin"


# ===== Logout =====

class TestLogout:
    """로그아웃 테스트."""

    async def test_logout_success(self, client: AsyncClient, admin_user, org):
        """로그아웃 후 리프레시 토큰 무효화."""
        login_res = await client.post(f"{ADMIN_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
        })
        refresh_token = login_res.json()["refresh_token"]

        # 로그아웃
        res = await client.post(f"{ADMIN_AUTH}/logout", json={
            "refresh_token": refresh_token,
        })
        assert res.status_code == 204

        # 로그아웃 후 리프레시 토큰 사용 불가
        res2 = await client.post(f"{ADMIN_AUTH}/refresh", json={
            "refresh_token": refresh_token,
        })
        assert res2.status_code == 401


# ===== /me Endpoint =====

class TestGetMe:
    """현재 사용자 프로필 조회 테스트."""

    async def test_get_me_success(self, client: AsyncClient, admin_user, admin_token):
        """인증된 사용자 정보 조회 성공."""
        res = await client.get(f"{ADMIN_AUTH}/me", headers=auth_header(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["username"] == "admin"
        assert data["role_name"] == "admin"
        assert data["role_level"] == 1
        assert data["is_active"] is True

    async def test_get_me_no_token(self, client: AsyncClient):
        """토큰 없이 /me 접근 시 403 (HTTPBearer)."""
        res = await client.get(f"{ADMIN_AUTH}/me")
        assert res.status_code == 403

    async def test_get_me_invalid_token(self, client: AsyncClient):
        """유효하지 않은 토큰으로 /me 접근 시 401."""
        res = await client.get(
            f"{ADMIN_AUTH}/me",
            headers=auth_header("invalid.jwt.token"),
        )
        assert res.status_code == 401

    async def test_get_me_staff_rejected(self, client: AsyncClient, staff_token):
        """스태프 토큰으로 admin /me 접근 시 403."""
        res = await client.get(f"{ADMIN_AUTH}/me", headers=auth_header(staff_token))
        assert res.status_code == 403


# ===== App Login =====

class TestAppLogin:
    """앱(직원) 로그인 테스트."""

    async def test_app_login_staff_success(self, client: AsyncClient, staff_user, org):
        """스태프 계정으로 앱 로그인 성공."""
        res = await client.post(f"{APP_AUTH}/login", json={
            "username": "staff",
            "password": "staff123!",
            "company_code": org.code,
        })
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data

    async def test_app_login_admin_also_works(self, client: AsyncClient, admin_user, org):
        """관리자 계정으로 앱 로그인도 가능."""
        res = await client.post(f"{APP_AUTH}/login", json={
            "username": "admin",
            "password": "admin123!",
            "company_code": org.code,
        })
        assert res.status_code == 200

    async def test_app_login_wrong_password(self, client: AsyncClient, staff_user, org):
        """잘못된 비밀번호로 앱 로그인 실패."""
        res = await client.post(f"{APP_AUTH}/login", json={
            "username": "staff",
            "password": "wrong",
            "company_code": org.code,
        })
        assert res.status_code == 401


# ===== App Register =====

class TestAppRegister:
    """앱 회원가입 테스트."""

    async def test_app_register_success(self, client: AsyncClient, org, roles):
        """앱 회원가입 성공 — 스태프 역할로 생성."""
        res = await client.post(f"{APP_AUTH}/register", json={
            "username": "newstaff",
            "password": "newpass123!",
            "full_name": "New Staff",
            "email": "new@test.com",
            "company_code": org.code,
        })
        assert res.status_code == 201
        data = res.json()
        assert "access_token" in data

    async def test_app_register_duplicate_username(self, client: AsyncClient, staff_user, org, roles):
        """중복 사용자명으로 회원가입 실패."""
        res = await client.post(f"{APP_AUTH}/register", json={
            "username": "staff",
            "password": "somepass123!",
            "full_name": "Another Staff",
            "company_code": org.code,
        })
        assert res.status_code == 409

    async def test_app_register_invalid_company_code(self, client: AsyncClient, org, roles):
        """잘못된 회사 코드로 회원가입 실패."""
        res = await client.post(f"{APP_AUTH}/register", json={
            "username": "newuser",
            "password": "pass123!",
            "full_name": "New User",
            "company_code": "BADCODE",
        })
        assert res.status_code == 404
