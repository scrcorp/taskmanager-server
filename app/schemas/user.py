"""사용자, 역할 및 프로필 관련 Pydantic 요청/응답 스키마 정의.

User, Role, and Profile Pydantic request/response schema definitions.
Covers CRUD operations for roles (permission levels), users
within an organization, and self-service profile management.
"""

from datetime import datetime
from pydantic import BaseModel


# === 역할 (Role) 스키마 ===

class RoleCreate(BaseModel):
    """역할 생성 요청 스키마.

    Role creation request schema.
    Creates a new role with a unique name and permission level within the org.

    Attributes:
        name: 역할 이름 (Role name, e.g. "manager", unique per org)
        level: 권한 레벨 (Permission level, 1=highest, unique per org)
    """

    name: str  # 역할 이름 — 조직 내 고유 (Role name, unique within org)
    level: int  # 권한 레벨 — 1=admin 최고, 4=staff 최저 (Permission level)


class RoleUpdate(BaseModel):
    """역할 수정 요청 스키마 (부분 업데이트).

    Role update request schema (partial update).
    Only provided fields are updated; omitted fields remain unchanged.

    Attributes:
        name: 역할 이름 (New role name, optional)
        level: 권한 레벨 (New permission level, optional)
    """

    name: str | None = None  # 변경할 역할 이름 (New name, optional)
    level: int | None = None  # 변경할 권한 레벨 (New level, optional)


class RoleResponse(BaseModel):
    """역할 응답 스키마.

    Role response schema returned from API.

    Attributes:
        id: 역할 UUID (Role unique identifier)
        name: 역할 이름 (Role name)
        level: 권한 레벨 (Permission level)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 역할 UUID 문자열 (Role UUID as string)
    name: str  # 역할 이름 (Role name)
    level: int  # 권한 레벨 (Permission level, 1=highest)
    created_at: datetime  # 생성 일시 (Creation timestamp in UTC)


# === 사용자 (User) 스키마 ===

class UserCreate(BaseModel):
    """사용자 생성 요청 스키마 (관리자용).

    User creation request schema (admin-only operation).
    Admin creates users with specific roles and credentials.

    Attributes:
        username: 로그인 아이디 (Login username, unique per org)
        password: 비밀번호 (Plain text, will be bcrypt-hashed)
        full_name: 실명 (Full display name)
        email: 이메일 (Email address, optional)
        role_id: 역할 UUID (Assigned role identifier)
    """

    username: str  # 로그인 아이디 — 조직 내 고유 (Login ID, unique within org)
    password: str  # 비밀번호 — 평문, 서버에서 해싱 (Plain text, hashed server-side)
    full_name: str  # 실명 (Full display name)
    email: str | None = None  # 이메일 (Optional email)
    role_id: str  # 역할 UUID 문자열 (Role UUID to assign)


class UserUpdate(BaseModel):
    """사용자 수정 요청 스키마 (부분 업데이트).

    User update request schema (partial update).
    Only provided fields are updated; omitted fields remain unchanged.

    Attributes:
        full_name: 실명 (New display name, optional)
        email: 이메일 (New email, optional)
        role_id: 역할 UUID (New role assignment, optional)
        is_active: 활성 상태 (Active status toggle, optional)
    """

    full_name: str | None = None  # 변경할 실명 (New name, optional)
    email: str | None = None  # 변경할 이메일 (New email, optional)
    role_id: str | None = None  # 변경할 역할 UUID (New role, optional)
    is_active: bool | None = None  # 활성 상태 변경 (Activate/deactivate, optional)


class UserResponse(BaseModel):
    """사용자 상세 응답 스키마.

    User detail response schema with role information.

    Attributes:
        id: 사용자 UUID (User unique identifier)
        username: 로그인 아이디 (Login username)
        full_name: 실명 (Full display name)
        email: 이메일 (Email, nullable)
        role_name: 역할 이름 (Resolved role name)
        role_level: 역할 레벨 (Resolved role level)
        is_active: 활성 상태 (Account active status)
        created_at: 생성 일시 (Account creation timestamp)
    """

    id: str  # 사용자 UUID 문자열 (User UUID as string)
    username: str  # 로그인 아이디 (Login username)
    full_name: str  # 실명 (Full display name)
    email: str | None  # 이메일 (Email, may be null)
    role_name: str  # 역할 이름 — 조인된 값 (Role name, resolved from Role table)
    role_level: int  # 역할 레벨 — 조인된 값 (Role level, resolved from Role table)
    is_active: bool  # 계정 활성 상태 (Account active flag)
    created_at: datetime  # 생성 일시 UTC (Account creation timestamp)


class UserListResponse(BaseModel):
    """사용자 목록 응답 스키마 (간략 버전).

    User list response schema (abbreviated version for list views).
    Excludes email and detailed timestamps for efficiency.

    Attributes:
        id: 사용자 UUID (User unique identifier)
        username: 로그인 아이디 (Login username)
        full_name: 실명 (Full display name)
        role_name: 역할 이름 (Resolved role name)
        is_active: 활성 상태 (Account active status)
    """

    id: str  # 사용자 UUID 문자열 (User UUID as string)
    username: str  # 로그인 아이디 (Login username)
    full_name: str  # 실명 (Full display name)
    role_name: str  # 역할 이름 — 조인된 값 (Role name, resolved from Role table)
    is_active: bool  # 계정 활성 상태 (Account active flag)


# === 프로필 (Profile) 스키마 ===


class ProfileResponse(BaseModel):
    """사용자 프로필 응답 스키마 (본인용).

    User profile response schema (for self-service).
    Returned when the current user retrieves their own profile.

    Attributes:
        id: 사용자 UUID (User UUID)
        username: 사용자 이름 (Username)
        full_name: 전체 이름 (Full display name)
        email: 이메일 주소, null 가능 (Email address, nullable)
        role_name: 역할 이름 (Role name, resolved from Role table)
        organization_id: 조직 UUID (Organization UUID)
    """

    id: str  # 사용자 UUID 문자열 (User UUID as string)
    username: str  # 로그인 아이디 (Login username)
    full_name: str  # 실명 (Full display name)
    email: str | None  # 이메일, null 가능 (Email, may be null)
    role_name: str  # 역할 이름 (Role name, resolved from Role table)
    organization_id: str  # 조직 UUID 문자열 (Organization UUID as string)


class ProfileUpdate(BaseModel):
    """사용자 프로필 업데이트 스키마 (본인용 부분 업데이트).

    User profile update schema (self-service partial update).
    Only provided fields are updated; omitted fields remain unchanged.

    Attributes:
        full_name: 새 전체 이름, 선택 (New full name, optional)
        email: 새 이메일, 선택 (New email, optional)
    """

    full_name: str | None = None  # 변경할 실명 (New name, optional)
    email: str | None = None  # 변경할 이메일 (New email, optional)
