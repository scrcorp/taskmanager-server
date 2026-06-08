"""사용자 및 역할 관련 SQLAlchemy ORM 모델 정의.

User and Role SQLAlchemy ORM model definitions.
Implements role-based access control (RBAC) with hierarchical levels
within each organization.

Tables:
    - roles: 조직 내 역할 (Roles within an organization, level-based hierarchy)
    - users: 사용자 계정 (User accounts with org/role scoping)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Index, Integer, Numeric, ForeignKey, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(Base):
    """역할 모델 — 조직 내 권한 수준을 정의.

    Role model — Defines permission priority within an organization.
    Lower priority numbers indicate higher authority:
        10 = owner, 20 = general_manager, 30 = supervisor, 40 = staff

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Parent organization foreign key)
        name: 역할 이름 (Role name, e.g. "owner", "staff")
        priority: 권한 우선순위 (Permission priority, 10=highest/owner)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        organization: 소속 조직 (Parent organization)
        users: 이 역할을 가진 사용자 목록 (Users assigned to this role)

    Constraints:
        uq_role_org_name: 조직 내 역할 이름 고유 (Unique role name per org)
        uq_role_org_priority: 조직 내 역할 우선순위 고유 (Unique role priority per org)
    """

    __tablename__ = "roles"

    # 역할 고유 식별자 — Role unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 역할도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 역할 이름 — Role display name (e.g. "owner", "general_manager", "supervisor", "staff")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 우선순위 — Priority (10=owner 최고 권한, 40=staff 최저 권한). 낮을수록 높은 권한.
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_role_org_name"),
        UniqueConstraint("organization_id", "priority", name="uq_role_org_priority"),
    )

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="roles")
    users = relationship("User", back_populates="role")
    role_permissions = relationship("RolePermission", back_populates="role", cascade="all, delete-orphan")


class User(Base):
    """사용자 모델 — 시스템 사용자 계정 정보.

    User model — System user account information.
    Each user belongs to exactly one organization and has one role.
    Username is unique within an organization (not globally).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Parent organization foreign key)
        role_id: 역할 FK (Assigned role foreign key)
        username: 로그인 아이디 (Login username, unique per org)
        email: 이메일 (Email address, optional)
        full_name: 실명 (Full display name)
        password_hash: bcrypt 해시된 비밀번호 (bcrypt-hashed password)
        is_active: 활성 상태 (Active status, soft-delete pattern)
        email_verified: 이메일 인증 여부 (Email verification status)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        organization: 소속 조직 (Parent organization)
        role: 사용자 역할 (Assigned role)
        refresh_tokens: 리프레시 토큰 목록 (Active refresh tokens, cascade delete)
        user_stores: 소속 매장 연결 (Store associations, cascade delete)

    Constraints:
        uq_user_org_username: 조직 내 사용자명 고유 (Unique username per org)
    """

    __tablename__ = "users"

    # 사용자 고유 식별자 — User unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 사용자도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 역할 FK — Assigned role (역할 삭제 시 제한됨, role deletion is restricted)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("roles.id"), nullable=False)
    # 로그인 아이디 — Login username (조직 내 고유, unique within org)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    # 이메일 — Email address (optional, for alerts)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 실명 — User's full display name
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 비밀번호 해시 — bcrypt hashed password (평문 저장 금지, never store plaintext)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 활성 상태 — Whether the user account is active
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 이메일 인증 여부 — Whether email has been verified
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # 로그인 실패 횟수 — Failed login attempt counter (reset on success)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 잠금 일시 — Timestamp when account was locked due to too many failed logins
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 비밀번호 변경 일시 — Last password change timestamp (for JWT invalidation)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 비밀번호 변경 권장 — Suggest password change on next login (not enforced)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # 기본 시급 — Default hourly rate for labor cost calculation (nullable)
    hourly_rate: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    # FOH/BOH 분류 — 직원의 근무 구역 카테고리 (Front/Back of House).
    # "FOH" = 홀/고객응대, "BOH" = 주방/후방, NULL = 미지정(오너·매니저 등 양쪽 아닌 경우).
    # 스케줄 탭 필터 + (향후) FOH/BOH별 인건비 집계용. 값 검증은 schema의 Pydantic Literal.
    department: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # 선호 언어 — Preferred UI/alert language (BCP-47 short code: en/es/ko).
    # 현재는 정보 수집용. 실제 UI 다국어화는 추후 별도 작업.
    preferred_language: Mapped[str] = mapped_column(String(8), nullable=False, default="en", server_default="en")
    # 알림 선호 — 카테고리별 in-app/email 활성화. 빈 객체(default) = 모두 on.
    # JSONB shape: { "<category_code>": { "in_app": bool, "email": bool } }
    # 헬퍼/카테고리 정의는 app/core/alert_categories.py 참조.
    alert_preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # 콘솔 UI 필터 영속 저장 — 페이지별 필터/검색/정렬 상태. 1계정 1데이터 (모든 디바이스 동일).
    # JSONB shape: { "<page_storage_key>": { "<param>": "<string>" } }
    # 예: {"users": {"q": "alice", "role": "staff"}, "tasks": {"page": "2"}}
    console_filters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # 근태 기기 PIN — 매장 공용 기기에서 clock in/out 시 사용하는 개인 PIN (현재 6자리, 추후 4~6 가변 예정).
    # Attendance device PIN. 조직 내 unique (NULL 다중 허용) — PIN 단독으로 user 식별 가능하게.
    clockin_pin: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    # 저장된 사인 이미지 — Storage key (S3 또는 local bucket).
    # IRS Form 4070 등 폼 서명에 재사용. 직원이 staff app Settings 에서 등록/변경.
    signature_image_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 사번 — Employee number (조직 내 non-null 값은 고유, NULL 다중 허용).
    # v1 은 read-only 표시 전용 (입력 UI / 자동 생성 없음, 기존 사용자는 전부 NULL).
    # 고유성은 partial unique index(uq_user_org_employee_no, WHERE employee_no IS NOT NULL).
    employee_no: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # 소프트 삭제 일시 — Timestamp when user was soft-deleted (NULL = active)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "username", name="uq_user_org_username"),
        UniqueConstraint("organization_id", "clockin_pin", name="uq_user_org_clockin_pin"),
        # 사번 — 조직 내 non-null 값만 고유. NULL 은 다중 허용 (partial unique).
        Index(
            "uq_user_org_employee_no",
            "organization_id",
            "employee_no",
            unique=True,
            postgresql_where=text("employee_no IS NOT NULL"),
        ),
    )

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="users")
    role = relationship("Role", back_populates="users")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")
    user_stores = relationship("UserStore", back_populates="user", cascade="all, delete-orphan")
