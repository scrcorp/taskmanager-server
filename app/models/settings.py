"""Settings Registry — 메타 기반 설정 시스템.

각 설정 키는 settings_registry에 정의되고, 실제 값은 org/store/staff 단위로
override 가능. resolver utility가 priority에 따라 최종 값을 결정한다.

Tables:
    - settings_registry: 메타 정의 (key, label, type, levels, default)
    - org_settings: 조직 단위 override (force_locked 가능)
    - store_settings: 매장 단위 override
    - staff_settings: 직원 단위 override
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SettingsRegistry(Base):
    """설정 메타 정의 — 새 설정 추가 시 row만 추가하면 됨."""

    __tablename__ = "settings_registry"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # value_type: number | boolean | string | json
    value_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # levels: 어느 레벨에서 override 가능한지. ["org", "store", "staff"]
    levels: Mapped[list] = mapped_column(JSONB, nullable=False)
    # default_priority: "item" (staff > store > org), "store", "org"
    default_priority: Mapped[str] = mapped_column(String(20), nullable=False, default="item")
    default_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class OrgSetting(Base):
    """조직 단위 설정 override."""

    __tablename__ = "org_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), ForeignKey("settings_registry.key", ondelete="CASCADE"), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # force_locked: True면 store/staff override 불가
    force_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_org_settings_org_key"),
    )


class StoreSetting(Base):
    """매장 단위 설정 override."""

    __tablename__ = "store_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), ForeignKey("settings_registry.key", ondelete="CASCADE"), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("store_id", "key", name="uq_store_settings_store_key"),
    )


class StaffSetting(Base):
    """직원 단위 설정 override."""

    __tablename__ = "staff_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), ForeignKey("settings_registry.key", ondelete="CASCADE"), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_staff_settings_user_key"),
    )
