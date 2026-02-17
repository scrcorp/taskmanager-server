"""인증 관련 Pydantic 요청/응답 스키마 정의.

Authentication-related Pydantic request/response schema definitions.
Covers login, registration, token issuance/refresh, and current user info.
"""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """관리자/직원 로그인 요청 스키마.

    Login request schema for admin and app authentication.
    Admin login rejects staff (level >= 4), app login allows staff + supervisor.

    Attributes:
        username: 사용자 로그인 아이디 (User login identifier)
        password: 비밀번호 (Plain text password, verified against bcrypt hash)
    """

    username: str  # 사용자 로그인 아이디 (User login identifier)
    password: str  # 비밀번호 — 평문, 서버에서 bcrypt 해시와 비교 (Plain text, compared to bcrypt hash)


class RegisterRequest(BaseModel):
    """앱 사용자 회원가입 요청 스키마.

    App user self-registration request schema.
    Creates a new user with the default staff role (level 4).

    Attributes:
        username: 사용자 아이디 (Desired login username)
        password: 비밀번호 (Plain text, will be bcrypt-hashed on server)
        full_name: 실명 (Full display name)
        email: 이메일 (Email address, optional)
    """

    username: str  # 사용자 아이디 — 조직 내 고유 (Login ID, unique within org)
    password: str  # 비밀번호 — 평문, 서버에서 bcrypt 해싱 (Plain text, server hashes with bcrypt)
    full_name: str  # 실명 (Full display name)
    email: str | None = None  # 이메일 (Optional email address)


class TokenResponse(BaseModel):
    """JWT 토큰 발급 응답 스키마.

    JWT token issuance response schema.
    Returned after successful login or token refresh.

    Attributes:
        access_token: JWT 액세스 토큰 (Short-lived access token)
        refresh_token: JWT 리프레시 토큰 (Long-lived refresh token)
        token_type: 토큰 유형 (Always "bearer" for Authorization header)
    """

    access_token: str  # JWT 액세스 토큰 — 만료: 30분 기본 (Access token, default TTL: 30min)
    refresh_token: str  # JWT 리프레시 토큰 — 만료: 7일 기본 (Refresh token, default TTL: 7 days)
    token_type: str = "bearer"  # 토큰 유형 — 항상 "bearer" (Token type for Authorization header)


class RefreshRequest(BaseModel):
    """토큰 갱신 요청 스키마.

    Token refresh request schema.
    Exchanges a valid refresh token for a new access/refresh token pair.

    Attributes:
        refresh_token: 기존 리프레시 토큰 (Existing refresh token to exchange)
    """

    refresh_token: str  # 기존 리프레시 토큰 (Current refresh token)


class UserMeResponse(BaseModel):
    """현재 사용자 정보 응답 스키마 (GET /me).

    Current user info response schema for the /me endpoint.
    Returns the authenticated user's profile with role and organization details.

    Attributes:
        id: 사용자 UUID (User unique identifier)
        username: 로그인 아이디 (Login username)
        full_name: 실명 (Full display name)
        email: 이메일 (Email, nullable)
        role_name: 역할 이름 (Role name, e.g. "admin")
        role_level: 역할 레벨 (Role level, 1=highest)
        organization_id: 소속 조직 UUID (Organization identifier)
        organization_name: 소속 조직 이름 (Organization name)
        is_active: 활성 상태 (Account active status)
    """

    id: str  # 사용자 UUID 문자열 (User UUID as string)
    username: str  # 로그인 아이디 (Login username)
    full_name: str  # 실명 (Full display name)
    email: str | None  # 이메일 (Email, may be null)
    role_name: str  # 역할 이름 (Role name, e.g. "admin", "staff")
    role_level: int  # 역할 레벨 — 1=admin, 4=staff (Permission level)
    organization_id: str  # 소속 조직 UUID 문자열 (Organization UUID as string)
    organization_name: str  # 소속 조직 이름 (Organization display name)
    is_active: bool  # 계정 활성 상태 (Whether the account is active)
