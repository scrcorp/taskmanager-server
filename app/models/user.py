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
from sqlalchemy import String, Boolean, DateTime, Integer, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(Base):
    """역할 모델 — 조직 내 권한 수준을 정의.

    Role model — Defines permission levels within an organization.
    Lower level numbers indicate higher authority:
        1 = owner, 2 = general_manager, 3 = supervisor, 4 = staff

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Parent organization foreign key)
        name: 역할 이름 (Role name, e.g. "owner", "staff")
        level: 권한 레벨 (Permission level, 1=highest)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        organization: 소속 조직 (Parent organization)
        users: 이 역할을 가진 사용자 목록 (Users assigned to this role)

    Constraints:
        uq_role_org_name: 조직 내 역할 이름 고유 (Unique role name per org)
        uq_role_org_level: 조직 내 역할 레벨 고유 (Unique role level per org)
    """

    __tablename__ = "roles"

    # 역할 고유 식별자 — Role unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 역할도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 역할 이름 — Role display name (e.g. "owner", "general_manager", "supervisor", "staff")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 권한 레벨 — Permission level (1=owner 최고 권한, 4=staff 최저 권한)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_role_org_name"),
        UniqueConstraint("organization_id", "level", name="uq_role_org_level"),
    )

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="roles")
    users = relationship("User", back_populates="role")


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
    # 이메일 — Email address (optional, for notifications)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 실명 — User's full display name
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 비밀번호 해시 — bcrypt hashed password (평문 저장 금지, never store plaintext)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 활성 상태 — Whether the user account is active
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 이메일 인증 여부 — Whether email has been verified
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "username", name="uq_user_org_username"),
    )

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="users")
    role = relationship("Role", back_populates="users")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")
    user_stores = relationship("UserStore", back_populates="user", cascade="all, delete-orphan")
