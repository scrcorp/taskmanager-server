"""조직 및 매장 관련 Pydantic 요청/응답 스키마 정의.

Organization and Store Pydantic request/response schema definitions.
Covers CRUD operations for organizations (tenants) and stores (locations).
"""

import re
from datetime import datetime
from typing import Any
from pydantic import BaseModel, field_validator

from app.models.organization import STORE_STATUSES, STORE_STATUS_OPEN

# 스토어 코드 — 파일명/식별용 짧은 약어 (예: IFO, SWC). org 내 유일(partial unique).
# 길이 2~10 영숫자. 현장에서 store 이름 약어(예: "swc - Seed Water Cafe")를 직접 붙이던
# 관행을 정식 필드로 흡수하기 위해 2-5 → 2-10 으로 완화 (2026-06-24).
_STORE_CODE_RE = re.compile(r"^[A-Z0-9]{2,10}$")


def _normalize_store_code(v: str | None) -> str | None:
    """trim → 대문자 → 빈문자는 None. 2~10 영숫자만 허용."""
    if v is None:
        return None
    v = v.strip().upper()
    if not v:
        return None
    if not _STORE_CODE_RE.match(v):
        raise ValueError("Store code must be 2-10 alphanumeric characters")
    return v


def _validate_store_status(v: str | None) -> str | None:
    """매장 상태값이 허용된 enum(preparing/open/paused/closed)인지 검증."""
    if v is None:
        return None
    v = v.strip().lower()
    if v not in STORE_STATUSES:
        raise ValueError(f"Store status must be one of {', '.join(STORE_STATUSES)}")
    return v


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
    default_hourly_rate: float | None = 0  # 기본 시급 (Default hourly rate). SV/Staff에는 redact되어 None.
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
    code: str | None = None  # 매장 코드 (Short code for filenames/identity, 2-10 alnum, optional)
    address: str | None = None  # 매장 주소 (Physical address, optional)
    phone: str | None = None  # 매장 연락처 (Store phone, optional)
    email: str | None = None  # 매장/매니저 이메일 (Store/manager email, optional)
    timezone: str | None = None  # IANA 타임존 (Store timezone override, optional)
    status: str = STORE_STATUS_OPEN  # 매장 상태 (preparing/open/paused/closed, default open)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate, optional)

    _norm_code = field_validator("code")(_normalize_store_code)
    _norm_status = field_validator("status")(_validate_store_status)


class StoreUpdate(BaseModel):
    """매장 수정 요청 스키마 (부분 업데이트).

    Store update request schema (partial update).

    Attributes:
        name: 매장 이름 (New name, optional)
        address: 매장 주소 (New address, optional)
        is_active: 활성 상태 (Active status toggle, optional)
    """

    name: str | None = None  # 변경할 매장 이름 (New name, optional)
    code: str | None = None  # 변경할 매장 코드 (New short code, 2-10 alnum, optional)
    address: str | None = None  # 변경할 주소 (New address, optional)
    phone: str | None = None  # 변경할 연락처 (New phone, optional)
    email: str | None = None  # 변경할 이메일 (New email, optional)
    status: str | None = None  # 매장 상태 변경 (preparing/open/paused/closed, optional)
    operating_hours: dict[str, Any] | None = None  # 운영시간 JSONB (Operating hours, optional)
    day_start_time: dict[str, str] | None = None  # 영업일 경계 시각 (Day boundary, optional)
    max_work_hours_weekly: int | None = None  # 주간 최대 근무시간 (Max weekly hours, optional)
    state_code: str | None = None  # 주(State) 코드 (US state code, optional)
    timezone: str | None = None  # IANA 타임존 (Store timezone override, optional)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate, optional)

    _norm_code = field_validator("code")(_normalize_store_code)
    _norm_status = field_validator("status")(_validate_store_status)


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
    code: str | None = None  # 매장 코드 (Short code for filenames/identity)
    address: str | None  # 매장 주소 (Address, may be null)
    phone: str | None = None  # 매장 연락처 (Store phone)
    email: str | None = None  # 매장/매니저 이메일 (Store/manager email)
    status: str = STORE_STATUS_OPEN  # 매장 상태 (preparing/open/paused/closed)
    sort_order: int = 0  # 정렬 순서 (Manual display order)
    is_active: bool  # 활성 상태(파생 = status==open). 구 필드 호환용 (Derived active flag)
    require_approval: bool = True  # 승인 필요 여부 (Schedule approval required)
    operating_hours: dict[str, Any] | None = None  # 운영시간 (Operating hours JSONB)
    day_start_time: dict[str, str] | None = None  # 영업일 경계 시각 (Day boundary JSONB)
    max_work_hours_weekly: int | None = None  # 주간 최대 근무시간 (Max weekly hours)
    state_code: str | None = None  # 주(State) 코드 (US state code)
    timezone: str | None = None  # IANA 타임존 (Store timezone override)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate)
    accepting_signups: bool = True  # 가입/지원 접수 여부 (Hiring signups open flag)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


class StoreReorderRequest(BaseModel):
    """매장 정렬 순서 일괄 변경 요청.

    Bulk store reorder request — store IDs in the desired display order.
    """

    store_ids: list[str]  # 새 순서의 매장 UUID 목록 (Store UUIDs in desired order)


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
