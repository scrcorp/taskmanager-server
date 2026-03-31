"""조직 및 매장 관련 Pydantic 요청/응답 스키마 정의.

Organization and Store Pydantic request/response schema definitions.
Covers CRUD operations for organizations (tenants) and stores (locations).
"""

from datetime import datetime
from typing import Any
from pydantic import BaseModel


# === 조직 (Organization) 스키마 ===

class OrganizationCreate(BaseModel):
    """조직 생성 요청 스키마.

    Organization creation request schema.

    Attributes:
        name: 조직 이름 (Organization display name)
    """

    name: str  # 조직 이름 (Organization display name)
    timezone: str = "America/Los_Angeles"  # IANA 타임존 (Organization timezone)


class OrganizationUpdate(BaseModel):
    """조직 수정 요청 스키마 (부분 업데이트).

    Organization update request schema (partial update).

    Attributes:
        name: 조직 이름 (New name, optional)
        is_active: 활성 상태 (Active status toggle, optional)
    """

    name: str | None = None  # 변경할 조직 이름 (New name, optional)
    is_active: bool | None = None  # 활성 상태 변경 (Activate/deactivate, optional)
    timezone: str | None = None  # IANA 타임존 (New timezone, optional)
    day_start_time: str | None = None  # 하루 기준 시작 시각 HH:MM (optional)
    weekly_overtime_limit: int | None = None  # 주간 OT 기준 시간 (optional)
    default_hourly_rate: float | None = None  # 기본 시급 (Default hourly rate, optional)


class OrganizationResponse(BaseModel):
    """조직 응답 스키마.

    Organization response schema returned from API.

    Attributes:
        id: 조직 UUID (Organization unique identifier)
        name: 조직 이름 (Organization name)
        is_active: 활성 상태 (Active status flag)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 조직 UUID 문자열 (Organization UUID as string)
    name: str  # 조직 이름 (Organization name)
    code: str  # 회사 코드 (Company code for staff app login)
    timezone: str  # IANA 타임존 (Organization timezone)
    day_start_time: str | None = None  # 하루 기준 시작 시각 (HH:MM)
    weekly_overtime_limit: int = 40  # 주간 OT 기준 시간
    default_hourly_rate: float = 0  # 기본 시급 (Default hourly rate)
    is_active: bool  # 활성 상태 (Active flag)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


# === 매장 (Store) 스키마 ===

class StoreCreate(BaseModel):
    """매장 생성 요청 스키마.

    Store creation request schema.
    Store is created under the authenticated user's organization.

    Attributes:
        name: 매장 이름 (Store name)
        address: 매장 주소 (Store address, optional)
    """

    name: str  # 매장 이름 (Store name)
    address: str | None = None  # 매장 주소 (Physical address, optional)
    timezone: str | None = None  # IANA 타임존 (Store timezone override, optional)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate, optional)


class StoreUpdate(BaseModel):
    """매장 수정 요청 스키마 (부분 업데이트).

    Store update request schema (partial update).

    Attributes:
        name: 매장 이름 (New name, optional)
        address: 매장 주소 (New address, optional)
        is_active: 활성 상태 (Active status toggle, optional)
    """

    name: str | None = None  # 변경할 매장 이름 (New name, optional)
    address: str | None = None  # 변경할 주소 (New address, optional)
    is_active: bool | None = None  # 활성 상태 변경 (Activate/deactivate, optional)
    operating_hours: dict[str, Any] | None = None  # 운영시간 JSONB (Operating hours, optional)
    day_start_time: dict[str, str] | None = None  # 영업일 경계 시각 (Day boundary, optional)
    max_work_hours_weekly: int | None = None  # 주간 최대 근무시간 (Max weekly hours, optional)
    state_code: str | None = None  # 주(State) 코드 (US state code, optional)
    timezone: str | None = None  # IANA 타임존 (Store timezone override, optional)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate, optional)


class StoreResponse(BaseModel):
    """매장 응답 스키마.

    Store response schema returned from API.

    Attributes:
        id: 매장 UUID (Store unique identifier)
        organization_id: 소속 조직 UUID (Parent organization)
        name: 매장 이름 (Store name)
        address: 매장 주소 (Store address, nullable)
        is_active: 활성 상태 (Active status flag)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 매장 UUID 문자열 (Store UUID as string)
    organization_id: str  # 소속 조직 UUID 문자열 (Organization UUID as string)
    name: str  # 매장 이름 (Store name)
    address: str | None  # 매장 주소 (Address, may be null)
    is_active: bool  # 활성 상태 (Active flag)
    require_approval: bool = True  # 승인 필요 여부 (Schedule approval required)
    operating_hours: dict[str, Any] | None = None  # 운영시간 (Operating hours JSONB)
    day_start_time: dict[str, str] | None = None  # 영업일 경계 시각 (Day boundary JSONB)
    max_work_hours_weekly: int | None = None  # 주간 최대 근무시간 (Max weekly hours)
    state_code: str | None = None  # 주(State) 코드 (US state code)
    timezone: str | None = None  # IANA 타임존 (Store timezone override)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


class StoreDetailResponse(StoreResponse):
    """매장 상세 응답 스키마 — 시간대/포지션 포함.

    Store detail response schema including nested shifts and positions.
    Used when full store context is needed (e.g. store detail page).

    Attributes:
        shifts: 소속 시간대 목록 (List of shifts under this store)
        positions: 소속 포지션 목록 (List of positions under this store)
    """

    shifts: list["ShiftResponse"] = []  # 소속 시간대 목록 (Shifts, default empty)
    positions: list["PositionResponse"] = []  # 소속 포지션 목록 (Positions, default empty)


# === 전방 참조용 내부 스키마 (Forward reference schemas) ===

class ShiftResponse(BaseModel):
    """매장 상세용 시간대 간략 응답 스키마.

    Abbreviated shift response for StoreDetailResponse nesting.

    Attributes:
        id: 시간대 UUID (Shift identifier)
        name: 시간대 이름 (Shift name)
        sort_order: 정렬 순서 (Display order)
    """

    id: str  # 시간대 UUID 문자열 (Shift UUID as string)
    name: str  # 시간대 이름 (Shift name)
    sort_order: int  # 정렬 순서 (Display order)


class PositionResponse(BaseModel):
    """매장 상세용 포지션 간략 응답 스키마.

    Abbreviated position response for StoreDetailResponse nesting.

    Attributes:
        id: 포지션 UUID (Position identifier)
        name: 포지션 이름 (Position name)
        sort_order: 정렬 순서 (Display order)
    """

    id: str  # 포지션 UUID 문자열 (Position UUID as string)
    name: str  # 포지션 이름 (Position name)
    sort_order: int  # 정렬 순서 (Display order)


# 전방 참조 해결 — Resolve forward references for StoreDetailResponse
StoreDetailResponse.model_rebuild()
