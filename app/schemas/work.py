"""근무 구성(시간대/포지션) 관련 Pydantic 요청/응답 스키마 정의.

Work configuration (Shift/Position) Pydantic request/response schema definitions.
Covers CRUD operations for shifts and positions scoped under a brand.
"""

from pydantic import BaseModel


# === 시간대 (Shift) 스키마 ===

class ShiftCreate(BaseModel):
    """시간대 생성 요청 스키마.

    Shift creation request schema.
    Shift is created under a specific brand (brand_id from URL path).

    Attributes:
        name: 시간대 이름 (Shift name, e.g. "오전", "오후")
        sort_order: 정렬 순서 (Display order, default 0)
    """

    name: str  # 시간대 이름 (Shift name)
    sort_order: int = 0  # 정렬 순서 — 낮을수록 먼저 표시 (Display order, lower = first)


class ShiftUpdate(BaseModel):
    """시간대 수정 요청 스키마 (부분 업데이트).

    Shift update request schema (partial update).

    Attributes:
        name: 시간대 이름 (New name, optional)
        sort_order: 정렬 순서 (New sort order, optional)
    """

    name: str | None = None  # 변경할 시간대 이름 (New name, optional)
    sort_order: int | None = None  # 변경할 정렬 순서 (New sort order, optional)


class ShiftResponse(BaseModel):
    """시간대 응답 스키마.

    Shift response schema returned from API.

    Attributes:
        id: 시간대 UUID (Shift unique identifier)
        brand_id: 소속 브랜드 UUID (Parent brand)
        name: 시간대 이름 (Shift name)
        sort_order: 정렬 순서 (Display order)
    """

    id: str  # 시간대 UUID 문자열 (Shift UUID as string)
    brand_id: str  # 소속 브랜드 UUID 문자열 (Brand UUID as string)
    name: str  # 시간대 이름 (Shift name)
    sort_order: int  # 정렬 순서 (Display order)


# === 포지션 (Position) 스키마 ===

class PositionCreate(BaseModel):
    """포지션 생성 요청 스키마.

    Position creation request schema.
    Position is created under a specific brand (brand_id from URL path).

    Attributes:
        name: 포지션 이름 (Position name, e.g. "그릴", "카운터")
        sort_order: 정렬 순서 (Display order, default 0)
    """

    name: str  # 포지션 이름 (Position name)
    sort_order: int = 0  # 정렬 순서 — 낮을수록 먼저 표시 (Display order, lower = first)


class PositionUpdate(BaseModel):
    """포지션 수정 요청 스키마 (부분 업데이트).

    Position update request schema (partial update).

    Attributes:
        name: 포지션 이름 (New name, optional)
        sort_order: 정렬 순서 (New sort order, optional)
    """

    name: str | None = None  # 변경할 포지션 이름 (New name, optional)
    sort_order: int | None = None  # 변경할 정렬 순서 (New sort order, optional)


class PositionResponse(BaseModel):
    """포지션 응답 스키마.

    Position response schema returned from API.

    Attributes:
        id: 포지션 UUID (Position unique identifier)
        brand_id: 소속 브랜드 UUID (Parent brand)
        name: 포지션 이름 (Position name)
        sort_order: 정렬 순서 (Display order)
    """

    id: str  # 포지션 UUID 문자열 (Position UUID as string)
    brand_id: str  # 소속 브랜드 UUID 문자열 (Brand UUID as string)
    name: str  # 포지션 이름 (Position name)
    sort_order: int  # 정렬 순서 (Display order)
