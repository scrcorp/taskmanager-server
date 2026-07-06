"""사용자, 역할 및 프로필 관련 Pydantic 요청/응답 스키마 정의.

User, Role, and Profile Pydantic request/response schema definitions.
Covers CRUD operations for roles (permission levels), users
within an organization, and self-service profile management.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 사번(employee_no) — 회사 사번. 문자열 유지(선행0 보존), org 내 유일(partial unique).
_EMPLOYEE_NO_RE = re.compile(r"^[A-Za-z0-9-]{1,50}$")


def _normalize_employee_no(v: str | None) -> str | None:
    """trim → 빈문자는 None. 영숫자+하이픈만, 선행0 보존(정수화 금지)."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if not _EMPLOYEE_NO_RE.match(v):
        raise ValueError("Employee number must be 1-50 alphanumeric/hyphen characters")
    return v


# username — 로그인 아이디. 3~30자, 영숫자로 시작, 이후 영숫자/`.`/`_`/`-` 허용.
# ('.' 하나 같은 무의미 아이디 방지.)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,29}$")


def _validate_username(v: str) -> str:
    """trim → 형식 검증. 3~30자, 영숫자 시작, 영숫자/./_/- 만."""
    v = (v or "").strip()
    if not _USERNAME_RE.match(v):
        raise ValueError(
            "Username must be 3-30 characters, start with a letter or digit, "
            "and use only letters, digits, dot, underscore, or hyphen"
        )
    return v


# === 역할 (Role) 스키마 ===

class RoleCreate(BaseModel):
    """역할 생성 요청 스키마.

    Role creation request schema.
    Creates a new role with a unique name and permission priority within the org.

    Attributes:
        name: 역할 이름 (Role name, e.g. "general_manager", unique per org)
        priority: 권한 우선순위 (Permission priority, 10=owner/highest, unique per org)
    """

    name: str
    priority: int  # 우선순위 — 10=owner, 40=staff (낮을수록 높은 권한)


class RoleUpdate(BaseModel):
    """역할 수정 요청 스키마 (부분 업데이트)."""

    name: str | None = None
    priority: int | None = None


class RoleResponse(BaseModel):
    """역할 응답 스키마."""

    id: str
    name: str
    priority: int
    created_at: datetime


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
    # 이름: first/middle/last (권장). full_name 은 없으면 셋을 합쳐 자동 생성(호환).
    full_name: str | None = None  # 실명 (없으면 first/middle/last 로 합성)
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    email: str | None = None  # 이메일 (Optional email)
    role_id: str  # 역할 UUID 문자열 (Role UUID to assign)
    department: Literal["FOH", "BOH"] | None = None  # FOH/BOH 분류 (None=미지정)
    employee_no: str | None = None  # 사번 (Company employee number, optional) [레거시]

    _norm_emp = field_validator("employee_no")(_normalize_employee_no)
    _valid_username = field_validator("username")(_validate_username)

    @model_validator(mode="after")
    def _compose_full_name(self) -> "UserCreate":
        """이름 규칙: first/last 경로면 둘 다 필수(middle 선택), full_name 합성.
        레거시로 full_name 만 직접 준 경우는 그대로 허용(호환)."""
        first = (self.first_name or "").strip()
        mid = (self.middle_name or "").strip()
        last = (self.last_name or "").strip()
        if first or last:
            if not first or not last:
                raise ValueError("First name and last name are required")
            self.full_name = " ".join(p for p in (first, mid, last) if p)
        if not (self.full_name and self.full_name.strip()):
            raise ValueError("Name is required")
        return self


class UserUpdate(BaseModel):
    """사용자 수정 요청 스키마 (부분 업데이트).

    User update request schema (partial update).
    Only provided fields are updated; omitted fields remain unchanged.

    Attributes:
        username: 로그인 아이디 (New username, optional)
        full_name: 실명 (New display name, optional)
        email: 이메일 (New email, optional)
        role_id: 역할 UUID (New role assignment, optional)
        is_active: 활성 상태 (Active status toggle, optional)
    """

    username: str | None = None  # 변경할 로그인 아이디 (New username, optional)
    full_name: str | None = None  # 변경할 실명 (New name, optional)
    email: str | None = None  # 변경할 이메일 (New email, optional)
    role_id: str | None = None  # 변경할 역할 UUID (New role, optional)
    is_active: bool | None = None  # 활성 상태 변경 (Activate/deactivate, optional)
    hourly_rate: float | None = None  # 개인 시급 (Personal hourly rate, optional)
    department: Literal["FOH", "BOH"] | None = None  # FOH/BOH 분류 변경 (None=미지정으로 해제)
    employee_no: str | None = None  # 사번 변경 (New employee number, optional)

    _norm_emp = field_validator("employee_no")(_normalize_employee_no)


class UserBulkUpdate(BaseModel):
    """여러 직원의 필드를 일괄 변경.

    Bulk-update fields for multiple users in one request.
    **보낸 필드만 적용** (model_fields_set 기준). 최소 1개 필드는 보내야 함.
    예) {"user_ids":[...], "department":"FOH", "is_active":false}
    department/hourly_rate 는 null 을 명시하면 "해제"(미지정/상속) 의미.

    NOTE: role_id / store 배정은 권한 가드·부수효과가 있어 이 스키마/경로에서
    다루지 않는다 (후속 증분에서 별도 처리).
    """

    user_ids: list[str] = Field(min_length=1)  # 대상 사용자 UUID 목록 (1개 이상)
    department: Literal["FOH", "BOH"] | None = None  # 보내면 설정 (None=미지정 해제)
    is_active: bool | None = None  # 보내면 활성/비활성 일괄 설정
    hourly_rate: float | None = None  # 보내면 시급 일괄 설정 (None=상속으로 해제)


class UserBulkUpdateResult(BaseModel):
    """일괄 변경 결과."""

    updated_count: int  # 실제 변경된 사용자 수


class UserResponse(BaseModel):
    """사용자 상세 응답 스키마.

    User detail response schema with role information.

    Attributes:
        id: 사용자 UUID (User unique identifier)
        username: 로그인 아이디 (Login username)
        full_name: 실명 (Full display name)
        email: 이메일 (Email, nullable)
        role_name: 역할 이름 (Resolved role name)
        role_priority: 역할 우선순위 (Resolved role priority)
        is_active: 활성 상태 (Account active status)
        created_at: 생성 일시 (Account creation timestamp)
    """

    id: str  # 사용자 UUID 문자열 (User UUID as string)
    username: str  # 로그인 아이디 (Login username)
    full_name: str  # 실명 (Full display name)
    email: str | None  # 이메일 (Email, may be null)
    email_verified: bool  # 이메일 인증 여부 (Email verification status)
    role_name: str  # 역할 이름 — 조인된 값 (Role name, resolved from Role table)
    role_priority: int  # 역할 우선순위 — 조인된 값
    hourly_rate: float | None = None  # 개인 시급 raw — NULL이면 상속 (None = inherit from store/org)
    effective_hourly_rate: float | None = None  # 실효 시급: user → (any store) → org cascade
    department: str | None = None  # FOH/BOH 분류 (None=미지정)
    employee_no: str | None = None  # 사번 (Company employee number, nullable) [레거시]
    crewid: int | None = None  # CREWID — org 안 1부터 순번 (org 번호)
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
    email: str | None = None  # 이메일 (Email, may be null)
    email_verified: bool = False  # 이메일 인증 여부 (Email verification status)
    role_name: str  # 역할 이름 — 조인된 값 (Role name, resolved from Role table)
    role_priority: int  # 역할 우선순위 — 조인된 값
    hourly_rate: float | None = None  # 개인 시급 raw — NULL이면 상속
    effective_hourly_rate: float | None = None  # 실효 시급: user → (any store) → org cascade
    department: str | None = None  # FOH/BOH 분류 (None=미지정)
    employee_no: str | None = None  # 사번 (Company employee number, nullable) [레거시]
    crewid: int | None = None  # CREWID — org 안 1부터 순번 (org 번호)
    is_active: bool  # 계정 활성 상태 (Account active flag)
    created_at: datetime  # 생성 일시 UTC (Account creation timestamp)


# === 매장 배정 (Store Assignment) 스키마 ===


class UserStoreAssignment(BaseModel):
    """매장 배정 항목."""
    store_id: str
    is_manager: bool = False
    is_work_assignment: bool = True


class SyncUserStoresRequest(BaseModel):
    """매장 배정 일괄 저장 요청."""
    assignments: list[UserStoreAssignment]


class UserStoreResponse(BaseModel):
    """매장 배정 응답 (is_manager + is_work_assignment)."""
    id: str
    organization_id: str
    name: str
    address: str | None
    is_active: bool
    is_manager: bool
    is_work_assignment: bool
    created_at: datetime
    empid: int | None = None  # 이 매장에서의 EMPID (매장 안 1부터 순번)


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
    preferred_language: str = "en"  # 선호 언어 (정보 수집용, default en)


class ProfileUpdate(BaseModel):
    """사용자 프로필 업데이트 스키마 (본인용 부분 업데이트).

    User profile update schema (self-service partial update).
    Only provided fields are updated; omitted fields remain unchanged.

    Attributes:
        username: 새 로그인 아이디, 선택 (New username, optional)
        full_name: 새 전체 이름, 선택 (New full name, optional)
        email: 새 이메일, 선택 (New email, optional)
        preferred_language: 선호 언어, 선택 (Preferred language, optional)
    """

    username: str | None = None  # 변경할 로그인 아이디 (New username, optional)
    full_name: str | None = None  # 변경할 실명 (New name, optional)
    email: str | None = None  # 변경할 이메일 (New email, optional)
    preferred_language: Literal["en", "es", "ko"] | None = None  # 선호 언어 (정보 수집용)


class AlertCategoryChannel(BaseModel):
    """카테고리 단일 채널 토글 — null 은 default(=on) 의미."""

    in_app: bool | None = None
    email: bool | None = None


class AlertCategoryMeta(BaseModel):
    """카테고리 메타 — 클라이언트 렌더용."""

    code: str
    label: str
    description: str
    email_available: bool


class AlertPreferencesResponse(BaseModel):
    """GET /me/alert-preferences 응답.

    카테고리 메타 + 사용자 현재 설정. 미명시 카테고리/채널은 default = True.
    """

    categories: list[AlertCategoryMeta]
    preferences: dict[str, AlertCategoryChannel]


class AlertPreferencesUpdate(BaseModel):
    """PUT /me/alert-preferences 요청.

    부분 업데이트 — 받은 필드만 적용. 알 수 없는 카테고리/채널은 server 가 무시.
    """

    preferences: dict[str, AlertCategoryChannel]
