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
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Index, Integer, Numeric, Text, Time, ForeignKey, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

# 매장 상태(라이프사이클) — Store lifecycle status values.
# preparing: 오픈 전 셋업 / open: 영업중(=구 is_active true) / paused: 일시중단 / closed: 폐점(soft-delete)
STORE_STATUS_PREPARING = "preparing"
STORE_STATUS_OPEN = "open"
STORE_STATUS_PAUSED = "paused"
STORE_STATUS_CLOSED = "closed"
STORE_STATUSES = (
    STORE_STATUS_PREPARING,
    STORE_STATUS_OPEN,
    STORE_STATUS_PAUSED,
    STORE_STATUS_CLOSED,
)

# 그룹 채번 모드 — Store group empid numbering mode.
# group: 그룹 내 모든 매장이 empid 시퀀스를 공유 / store: 그룹에 속해도 매장별 독립 시퀀스
NUMBERING_MODE_GROUP = "group"
NUMBERING_MODE_STORE = "store"
NUMBERING_MODES = (NUMBERING_MODE_GROUP, NUMBERING_MODE_STORE)


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
    # IANA 타임존 — Organization default timezone (e.g. "America/Los_Angeles")
    timezone: Mapped[str] = mapped_column(String(50), default="America/Los_Angeles")
    # 하루 기준 시작 시각 — Day boundary start time for timeline/reports (default 08:00)
    day_start_time: Mapped[Optional[datetime]] = mapped_column(Time(), nullable=True, default=None)
    # 주간 OT 기준 시간 — Weekly overtime threshold in hours (default 40, store can override)
    weekly_overtime_limit: Mapped[int] = mapped_column(Integer, default=40)
    # 기본 시급 — Organization default hourly rate (fallback, default 0)
    default_hourly_rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    # 활성 상태 — Whether the organization is active (soft-delete pattern)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 소프트 삭제 일시 — Timestamp when organization was soft-deleted (NULL = active)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships (cascade: 조직 삭제 시 하위 데이터 일괄 삭제)
    stores = relationship("Store", back_populates="organization", cascade="all, delete-orphan")
    roles = relationship("Role", back_populates="organization", cascade="all, delete-orphan")
    # User→Organization FK 가 2개(organization_id, last_org_id)라 명시 필요.
    users = relationship("User", back_populates="organization", cascade="all, delete-orphan", foreign_keys="User.organization_id")


class StoreGroup(Base):
    """매장 그룹 모델 — 조직 하위에서 매장들을 묶는 단위 (브랜드/지역 등).

    Store group model — Groups stores within an organization.
    empid 채번 정책의 단위: numbering_mode="group" 이면 그룹 내 모든 매장이
    empid 시퀀스를 공유하고, "store" 면 그룹에 속해도 매장별 독립 시퀀스.
    number_range_start 는 그룹 기본 번호대 시작값(예: 1000) — 매장별 값이 우선.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Parent organization)
        name: 그룹 이름 (Group display name)
        sort_order: 그룹 표시 순서 (Manual display order, drag-reorder)
        numbering_mode: 채번 모드 group|store (empid sequence scope)
        number_range_start: 그룹 기본 번호대 시작값 (Default empid range start)
        created_at/updated_at: 타임스탬프 UTC (Timestamps)
    """

    __tablename__ = "store_groups"

    # 그룹 고유 식별자 — Group unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 그룹도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    # 그룹 이름 — Group display name
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 그룹 코드 — 급여/외부 시스템에서 이 법인을 부르는 짧은 표기 (예: "ODG").
    # 매장 code 와 같은 역할의 그룹판. EMPID 임포트의 자연 매칭 키로도 쓰인다.
    # ⚠️ 미머지 payroll 브랜치의 StoreGroup.payroll_code 와 같은 개념 — 그쪽 머지 시
    # 이 필드로 통합할 것 (인수인계 문서 §2-1 이 지목한 중복 후보).
    code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 정렬 순서 — Manual display order within org (lower first). Drag-reorder.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 채번 모드 — empid sequence scope: "group"(그룹 공유, 기본) | "store"(매장별 독립)
    numbering_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default=NUMBERING_MODE_GROUP, server_default=NUMBERING_MODE_GROUP
    )
    # 그룹 기본 번호대 시작값 — Default empid range start (e.g. 1000). 매장 값이 우선. NULL=1부터.
    number_range_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 다음 발급 empid — 그룹 공유 스코프(numbering_mode="group")의 채번 커서.
    # 채번은 이 값에서 시작한다. MAX(empid) 를 쓰지 않는 이유: 예외 번호(본사 이관 등)가
    # 순번을 끌고 올라가기 때문. 전진만 하고(INV-2), 낮추는 것은 운영자 수동 조정만 허용.
    # NULL = 아직 백필 전(마이그레이션이 전부 채운다 — 코드에 NULL 폴백을 두지 않는다).
    next_empid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 급여 파일 표시명 — corp column value in the payroll export (e.g. "M KOREAN BBQ").
    # NULL 이면 export 시 group.name 으로 폴백. 시트/파일 식별자는 code 필드가 담당
    # (구 payroll_code 는 code 로 통합 — 둘 다 짧은 그룹 식별자라 역할이 같았다).
    payroll_corp_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships (SET NULL: 그룹 삭제 시 매장은 미그룹으로)
    organization = relationship("Organization")
    stores = relationship("Store", back_populates="group")


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
    # 매장 코드 — Short identifier for the store (unique within org, e.g. "DT", "GM")
    code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 매장 연락처 — Store phone number (optional, shown on public signup/hiring page)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # 매장/매니저 이메일 — Store or manager email (optional, signup/escalation/notification target)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 매장 주소 — Physical address of the store (optional)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IANA 타임존 — Store-level timezone override (nullable, falls back to org timezone)
    timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 매장 상태 — Store lifecycle status (preparing/open/paused/closed). SoT for active/retire.
    # 구 is_active 컬럼을 대체. is_active 는 아래 hybrid_property(= status==open)로 파생.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STORE_STATUS_OPEN, server_default=STORE_STATUS_OPEN
    )
    # 정렬 순서 — Manual display order within org (lower first, then created_at). Drag-reorder.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 소속 그룹 FK — Store group (SET NULL: 그룹 삭제 시 미그룹으로). NULL=미그룹.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("store_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 매장 번호대 시작값 — empid range start override (그룹 값보다 우선). NULL=그룹/1 폴백.
    number_range_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 다음 발급 empid — 매장 단독 스코프(미그룹 or 그룹 numbering_mode="store")의 채번 커서.
    # 그룹 공유 매장은 그룹 커서를 쓰므로 이 값이 쉬고 있다가, 그룹에서 빠지거나
    # 모드가 바뀌면 다시 쓰인다(그래서 백필도 전 매장을 채운다). 규칙은 그룹 커서와 동일.
    next_empid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 소프트 삭제 일시 — Timestamp when store was soft-deleted/closed (NULL = live)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 승인 필요 여부 — Whether schedule approval is required (default True)
    # True: SV가 생성한 스케줄은 GM 승인 후 배정 생성
    # False: SV가 생성하면 즉시 배정 생성
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    # 운영시간(영업시간)은 여기 없다 — settings registry 키 `store.operating_hours` 로 옮겼다 (D2-3).
    # 컬럼 시절엔 미설정이 NULL 이라 호출부마다 폴백을 각자 짜야 했고, 결국 전 매장 NULL 인 채로
    # 방치돼 리포트의 영업시간 필터가 사실상 아무 일도 하지 않았다. registry 는 cascade 를 준다.
    # 영업일 경계 시각 — Day boundary start time per weekday (JSONB)
    # Format: {"all": "06:00"} or {"mon": "06:00", "tue": "07:00", ...}
    # NOT operating hours — defines when a work day starts for attendance/schedule purposes.
    day_start_time: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 주간 최대 근무시간 — Maximum weekly work hours for this store
    max_work_hours_weekly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 주(State) 코드 — US state code for labor law compliance (e.g. "CA", "NY")
    state_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 매장 기본 시급 — Store default hourly rate (overrides org rate, null = use org rate)
    default_hourly_rate: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    # 공개 가입 링크 활성화 여부 — Whether the public signup link (/join/{encoded}) accepts new hires.
    # When false, GET /app/auth/stores/by-code/{encoded} returns "signups_paused".
    accepting_signups: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # 매장 표지 사진 — Cover photos for the public signup page (JSONB array).
    # Format: [{"key": "stores/{store_id}/cover/{uuid}.jpg", "is_primary": bool, "uploaded_at": "ISO", "size": int}]
    # DB stores only relative keys; URLs resolved at runtime via storage_service.resolve_url(key).
    cover_photos: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 활성 상태(파생) — Derived from status. 구 is_active 컬럼 호환용.
    # Python 읽기(store.is_active)와 SQL 필터(Store.is_active.is_(True)) 모두 지원.
    @hybrid_property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.status == STORE_STATUS_OPEN

    @is_active.expression  # type: ignore[no-redef]
    def is_active(cls):  # noqa: N805
        return cls.status == STORE_STATUS_OPEN

    # 관계 — Relationships
    organization = relationship("Organization", back_populates="stores")
    group = relationship("StoreGroup", back_populates="stores")
    shifts = relationship("Shift", back_populates="store", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="store", cascade="all, delete-orphan")
    user_stores = relationship("UserStore", back_populates="store", cascade="all, delete-orphan")

    __table_args__ = (
        # 스토어 코드는 org 내 유일 (파일명 식별자 충돌 방지). partial:
        # code NULL(미부여) 다수 허용 + soft-delete 된 스토어는 코드 반납.
        Index(
            "uq_store_org_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("code IS NOT NULL AND deleted_at IS NULL"),
        ),
    )


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
