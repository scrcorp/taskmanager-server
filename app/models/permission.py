"""Permission 및 RolePermission SQLAlchemy ORM 모델 정의.

Permission-Based RBAC를 위한 권한 및 역할-권한 매핑 테이블.

Tables:
    - permissions: 글로벌 권한 목록 (resource:action 형식)
    - role_permissions: 역할별 권한 매핑 (role ↔ permission)
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Permission(Base):
    """권한 모델 — 시스템 전체 권한 정의.

    Attributes:
        id: 고유 식별자 UUID
        code: 권한 코드 (e.g. "stores:read")
        resource: 리소스명 (e.g. "stores")
        action: 액션명 (e.g. "read")
        description: 한글 설명
        require_priority_check: priority 비교 필요 여부
        created_at: 생성 일시
    """

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    resource: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    require_priority_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("resource", "action", name="idx_permissions_resource_action"),
    )

    role_permissions = relationship("RolePermission", back_populates="permission")


class RolePermission(Base):
    """역할-권한 매핑 모델.

    Attributes:
        id: 고유 식별자 UUID
        role_id: 역할 FK
        permission_id: 권한 FK
        created_at: 생성 일시
    """

    __tablename__ = "role_permissions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )

    role = relationship("Role", back_populates="role_permissions")
    permission = relationship("Permission", back_populates="role_permissions")
