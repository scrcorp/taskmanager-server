"""인증 관련 Pydantic 요청/응답 스키마 정의.

Authentication-related Pydantic request/response schema definitions.
Covers login, registration, token issuance/refresh, and current user info.
"""

from typing import Literal

from pydantic import BaseModel

# 지원 선호 언어 — Supported preferred language codes (BCP-47 short).
# 현재는 정보 수집용. UI 다국어화는 추후 별도 작업.
PreferredLanguage = Literal["en", "es", "ko"]


class LoginRequest(BaseModel):
    """관리자/직원 로그인 요청 스키마.

    Login request schema for admin and app authentication.
    Admin login rejects staff (level >= 4), app login allows staff + supervisor.

    Attributes:
        username: 사용자 로그인 아이디 (User login identifier)
        password: 비밀번호 (Plain text password, verified against bcrypt hash)
        company_code: 회사 코드 (Company code to identify organization, optional)
    """

    username: str  # 사용자 로그인 아이디 (User login identifier)
    password: str  # 비밀번호 — 평문, 서버에서 bcrypt 해시와 비교 (Plain text, compared to bcrypt hash)
    company_code: str | None = None  # 회사 코드 — 조직 식별용 (Company code for org identification)


class RegisterRequest(BaseModel):
    """앱 사용자 회원가입 요청 스키마.

    App user self-registration request schema.
    Creates a new user with the default staff role (priority 40).

    Attributes:
        username: 사용자 아이디 (Desired login username)
        password: 비밀번호 (Plain text, will be bcrypt-hashed on server)
        full_name: 실명 (Full display name)
        email: 이메일 (Email address, optional)
        company_code: 회사 코드 (Company code to identify organization)
    """

    username: str  # 사용자 아이디 — 조직 내 고유 (Login ID, unique within org)
    password: str  # 비밀번호 — 평문, 서버에서 bcrypt 해싱 (Plain text, server hashes with bcrypt)
    full_name: str  # 실명 (Full display name)
    email: str  # 이메일 — 필수 (Email address, required for verification)
    company_code: str | None = None  # 회사 코드 — 미지정 시 단일 org 자동 매칭
    verification_token: str  # 이메일 인증 토큰 — 코드 검증 성공 시 발급 (Issued after code verification)
    store_ids: list[str] = []  # 배정할 매장 ID 목록 (Store UUIDs to assign user to)
    preferred_language: PreferredLanguage = "en"  # 선호 언어 (정보 수집용, default en)


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


class SwitchOrgRequest(BaseModel):
    """org 컨텍스트 전환 요청 (POST /auth/switch-org)."""

    organization_id: str


class OrgMembershipInfo(BaseModel):
    """사용자의 org 소속 1건 + 접근 상태 (org 스위처/차단화면용)."""

    organization_id: str
    organization_name: str | None = None
    organization_code: str | None = None
    role_name: str | None = None
    role_priority: int | None = None
    member_status: str
    license_status: str | None = None
    accessible: bool
    block_reason: str | None = None  # None=접근가능, ORG_LICENSE_INACTIVE/ORG_ACCESS_REVOKED 등


class UserMeResponse(BaseModel):
    """현재 사용자 정보 응답 스키마 (GET /me).

    Current user info response schema for the /me endpoint.
    Returns the authenticated user's profile with role and organization details.

    Attributes:
        id: 사용자 UUID (User unique identifier)
        username: 로그인 아이디 (Login username)
        full_name: 실명 (Full display name)
        email: 이메일 (Email, nullable)
        role_name: 역할 이름 (Role name, e.g. "owner")
        role_priority: 역할 우선순위 (10=owner, 40=staff)
        organization_id: 소속 조직 UUID (Organization identifier)
        organization_name: 소속 조직 이름 (Organization name)
        is_active: 활성 상태 (Account active status)
    """

    id: str
    username: str
    full_name: str
    email: str | None
    email_verified: bool
    role_name: str
    role_priority: int  # 역할 우선순위 — 10=owner, 40=staff (낮을수록 높은 권한)
    organization_id: str
    organization_name: str
    company_code: str
    organization_timezone: str  # 조직 IANA 타임존 (Organization timezone)
    is_active: bool
    must_change_password: bool = False  # 비밀번호 변경 권장 여부
    permissions: list[str] = []  # 역할에 할당된 permission code 목록
    preferred_language: PreferredLanguage = "en"  # 선호 언어 (정보 수집용, default en)
    # 콘솔 페이지별 영속 필터/검색/정렬 상태 — 1계정 1데이터, 다른 디바이스에서도 동일.
    # shape: { "<page_storage_key>": { "<param>": "<string>" } }
    console_filters: dict[str, dict[str, str]] = {}
    # [Model B] 이 계정이 소속된 모든 org + 각 접근상태 (org 스위처/차단화면용).
    organizations: list[OrgMembershipInfo] = []
    # 현재(선택된) org 접근 가능 여부 + 차단 이유 코드. /me 는 차단돼도 200 으로 이걸 알려준다.
    current_org_accessible: bool = True
    current_org_block_reason: str | None = None


class ConsoleFiltersUpdateRequest(BaseModel):
    """콘솔 필터 전체 교체 요청 — PUT /auth/me/console-filters.

    Replaces the entire console_filters JSONB blob with the provided value.
    The console always sends the full object (last-write-wins).
    """

    filters: dict[str, dict[str, str]]


class ConsoleFiltersResponse(BaseModel):
    """콘솔 필터 응답."""

    console_filters: dict[str, dict[str, str]]


# ── Find Username ──

class FindUsernameRequest(BaseModel):
    email: str

class FindUsernameResponse(BaseModel):
    masked_username: str

class FindUsernameSendCodeRequest(BaseModel):
    email: str

class FindUsernameVerifyCodeRequest(BaseModel):
    email: str
    code: str

class FindUsernameVerifyResponse(BaseModel):
    username: str


# ── Reset Password ──

class ResetPasswordSendCodeRequest(BaseModel):
    username: str
    email: str

class ResetPasswordVerifyCodeRequest(BaseModel):
    email: str
    code: str

class ResetPasswordVerifyResponse(BaseModel):
    reset_token: str

class ResetPasswordConfirmRequest(BaseModel):
    reset_token: str
    new_password: str


# ── Change Password ──

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class ChangePasswordResponse(BaseModel):
    access_token: str
    refresh_token: str
    message: str


# ── Admin Reset Password ──

class AdminResetPasswordResponse(BaseModel):
    temporary_password: str
    message: str
