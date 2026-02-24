"""조직 관련 SQLAlchemy ORM 모델 정의.

Organization-related SQLAlchemy ORM model definitions.
Includes Organization (tenant) and Store (sub-business) entities
with cascade delete relationships.

Tables:
    - organizations: 최상위 테넌트 (Top-level tenant)
    - stores: 조직 하위 매장 (Store under organization)
"""

import random
import string
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Integer, Text, Time, ForeignKey, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship


def generate_company_code() -> str:
    """6자리 랜덤 회사 코드 생성 (대문자 + 숫자).

    Generate a random 6-character company code (uppercase letters + digits).
    """
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=6))

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
        stores: 소속 매장 목록 (List of child stores, cascade delete)
        roles: 조직 내 역할 목록 (List of roles in this org, cascade delete)
        users: 조직 내 사용자 목록 (List of users in this org, cascade delete)
    """

    __tablename__ = "organizations"

    # 조직 고유 식별자 — Organization unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 조직 이름 — Organization display name (max 255 chars, required)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 회사 코드 — Short unique company code for staff app login (6 chars, uppercase + digits)
    code: Mapped[str] = mapped_column(String(6), unique=True, nullable=False, default=generate_company_code)
    # 활성 상태 — Whether the organization is active (soft-delete pattern)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships (cascade: 조직 삭제 시 하위 데이터 일괄 삭제)
    stores = relationship("Store", back_populates="organization", cascade="all, delete-orphan")
    roles = relationship("Role", back_populates="organization", cascade="all, delete-orphan")
    users = relationship("User", back_populates="organization", cascade="all, delete-orphan")


class Store(Base):
    """매장 모델 — 조직 하위의 사업장 단위.

    Store (business unit) model — Sub-entity under an Organization.
    Represents a physical location or business line. Shifts, positions,
    and user assignments are scoped to a store.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Parent organization foreign key)
        name: 매장 이름 (Store name)
        address: 매장 주소 (Store address, optional)
        is_active: 활성 상태 (Active status flag)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        organization: 소속 조직 (Parent organization)
        shifts: 근무 시간대 목록 (Shift schedules under this store)
        positions: 포지션 목록 (Work positions under this store)
        user_stores: 소속 사용자 연결 (User-store associations)
    """

    __tablename__ = "stores"

    # 매장 고유 식별자 — Store unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 매장도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 매장 이름 — Store display name
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 매장 주소 — Physical address of the store (optional)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 활성 상태 — Whether the store is active (soft-delete pattern)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 승인 필요 여부 — Whether schedule approval is required (default True)
    # True: SV가 생성한 스케줄은 GM 승인 후 배정 생성
    # False: SV가 생성하면 즉시 배정 생성
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    # 운영시간 — Store operating hours as JSONB (e.g. {"mon": {"open": "09:00", "close": "22:00"}, ...})
    operating_hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 주간 최대 근무시간 — Maximum weekly work hours for this store
    max_work_hours_weekly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 주(State) 코드 — US state code for labor law compliance (e.g. "CA", "NY")
    state_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="stores")
    shifts = relationship("Shift", back_populates="store", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="store", cascade="all, delete-orphan")
    user_stores = relationship("UserStore", back_populates="store", cascade="all, delete-orphan")


class ShiftPreset(Base):
    """시프트 프리셋 모델 — 매장+시프트 조합별 시간 프리셋.

    Shift preset model — Predefined time ranges for a store's shift.
    Used to quickly assign schedules with preset start/end times.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK
        store_id: 소속 매장 FK
        shift_id: 연결 시프트 FK
        name: 프리셋 이름 (e.g. "오전 풀타임")
        start_time: 시작 시간
        end_time: 종료 시간
        is_active: 활성 상태
        sort_order: 정렬 순서
    """

    __tablename__ = "shift_presets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[datetime] = mapped_column(Time(), nullable=False)
    end_time: Mapped[datetime] = mapped_column(Time(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    store = relationship("Store", foreign_keys=[store_id])
    shift = relationship("Shift", foreign_keys=[shift_id])


class LaborLawSetting(Base):
    """노동법 설정 모델 — 매장별 초과근무/노동법 기준값.

    Labor law setting model — Per-store overtime and labor law thresholds.
    Used for overtime warnings when creating schedules.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK
        store_id: 소속 매장 FK
        federal_max_weekly: 연방 주간 최대시간 (기본 40)
        state_max_weekly: 주(State)별 최대시간
        store_max_weekly: 매장 자체 최대시간
        overtime_threshold_daily: 일일 초과근무 기준시간
    """

    __tablename__ = "labor_law_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    federal_max_weekly: Mapped[int] = mapped_column(Integer, default=40)
    state_max_weekly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    store_max_weekly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overtime_threshold_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    store = relationship("Store", foreign_keys=[store_id])
