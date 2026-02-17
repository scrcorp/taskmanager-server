"""조직 관련 SQLAlchemy ORM 모델 정의.

Organization-related SQLAlchemy ORM model definitions.
Includes Organization (tenant) and Brand (sub-business) entities
with cascade delete relationships.

Tables:
    - organizations: 최상위 테넌트 (Top-level tenant)
    - brands: 조직 하위 브랜드/매장 (Sub-business/store under organization)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Text, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Organization(Base):
    """조직(테넌트) 모델 — 시스템의 최상위 엔티티.

    Organization (tenant) model — Top-level entity in the system.
    All data is scoped under an organization for multi-tenant isolation.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        name: 조직 이름 (Organization name)
        is_active: 활성 상태 (Active status flag)
        created_at: 생성 일시 UTC (Creation timestamp in UTC)
        updated_at: 수정 일시 UTC (Last update timestamp in UTC)

    Relationships:
        brands: 소속 브랜드 목록 (List of child brands, cascade delete)
        roles: 조직 내 역할 목록 (List of roles in this org, cascade delete)
        users: 조직 내 사용자 목록 (List of users in this org, cascade delete)
    """

    __tablename__ = "organizations"

    # 조직 고유 식별자 — Organization unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 조직 이름 — Organization display name (max 255 chars, required)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 활성 상태 — Whether the organization is active (soft-delete pattern)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships (cascade: 조직 삭제 시 하위 데이터 일괄 삭제)
    brands = relationship("Brand", back_populates="organization", cascade="all, delete-orphan")
    roles = relationship("Role", back_populates="organization", cascade="all, delete-orphan")
    users = relationship("User", back_populates="organization", cascade="all, delete-orphan")


class Brand(Base):
    """브랜드(매장) 모델 — 조직 하위의 사업장 단위.

    Brand (store/business unit) model — Sub-entity under an Organization.
    Represents a physical location or business line. Shifts, positions,
    and user assignments are scoped to a brand.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Parent organization foreign key)
        name: 브랜드/매장 이름 (Brand/store name)
        address: 매장 주소 (Store address, optional)
        is_active: 활성 상태 (Active status flag)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        organization: 소속 조직 (Parent organization)
        shifts: 근무 시간대 목록 (Shift schedules under this brand)
        positions: 포지션 목록 (Work positions under this brand)
        user_brands: 소속 사용자 연결 (User-brand associations)
    """

    __tablename__ = "brands"

    # 브랜드 고유 식별자 — Brand unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 브랜드도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 브랜드 이름 — Brand/store display name
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 매장 주소 — Physical address of the store (optional)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 활성 상태 — Whether the brand is active (soft-delete pattern)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="brands")
    shifts = relationship("Shift", back_populates="brand", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="brand", cascade="all, delete-orphan")
    user_brands = relationship("UserBrand", back_populates="brand", cascade="all, delete-orphan")
