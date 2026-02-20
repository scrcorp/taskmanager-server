"""조직 및 매장 관련 Pydantic 요청/응답 스키마 정의.

Organization and Store Pydantic request/response schema definitions.
Covers CRUD operations for organizations (tenants) and stores (locations).
"""

from datetime import datetime
from pydantic import BaseModel


# === 조직 (Organization) 스키마 ===

class OrganizationCreate(BaseModel):
    """조직 생성 요청 스키마.

    Organization creation request schema.

    Attributes:
        name: 조직 이름 (Organization display name)
    """

    name: str  # 조직 이름 (Organization display name)


class OrganizationUpdate(BaseModel):
    """조직 수정 요청 스키마 (부분 업데이트).

    Organization update request schema (partial update).

    Attributes:
        name: 조직 이름 (New name, optional)
        is_active: 활성 상태 (Active status toggle, optional)
    """

    name: str | None = None  # 변경할 조직 이름 (New name, optional)
    is_active: bool | None = None  # 활성 상태 변경 (Activate/deactivate, optional)


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
