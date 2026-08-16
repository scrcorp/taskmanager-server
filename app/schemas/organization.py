"""조직 및 매장 관련 Pydantic 요청/응답 스키마 정의.

Organization and Store Pydantic request/response schema definitions.
Covers CRUD operations for organizations (tenants) and stores (locations).
"""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.organization import (
    NUMBERING_MODE_GROUP,
    NUMBERING_MODES,
    STORE_STATUSES,
    STORE_STATUS_OPEN,
)

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


def _validate_numbering_mode(v: str | None) -> str | None:
    """그룹 채번 모드가 허용된 enum(group/store)인지 검증."""
    if v is None:
        return None
    v = v.strip().lower()
    if v not in NUMBERING_MODES:
        raise ValueError(f"Numbering mode must be one of {', '.join(NUMBERING_MODES)}")
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


# === 매장 그룹 (StoreGroup) 스키마 ===

class StoreGroupCreate(BaseModel):
    """매장 그룹 생성 요청 스키마.

    Store group creation request schema.

    Attributes:
        name: 그룹 이름 (Group name)
        numbering_mode: 채번 모드 group|store (empid sequence scope, default group)
        number_range_start: 그룹 기본 번호대 시작값 (Default empid range start, optional)
    """

    name: str  # 그룹 이름 (Group name)
    # 그룹 코드 — 급여/외부 시스템 표기 (예: "ODG"). 임포트 자연 매칭 키
    code: str | None = Field(default=None, max_length=20)
    numbering_mode: str = NUMBERING_MODE_GROUP  # 채번 모드 (group=공유 시퀀스, store=매장별)
    number_range_start: int | None = Field(default=None, ge=1)  # 번호대 시작값 (예: 1000)

    _norm_mode = field_validator("numbering_mode")(_validate_numbering_mode)


class StoreGroupUpdate(BaseModel):
    """매장 그룹 수정 요청 스키마 (부분 업데이트).

    Store group update request schema (partial update).
    """

    name: str | None = None  # 변경할 그룹 이름 (New name, optional)
    code: str | None = Field(default=None, max_length=20)  # 그룹 코드 (예: "ODG")
    numbering_mode: str | None = None  # 채번 모드 변경 (group|store, optional)
    number_range_start: int | None = Field(default=None, ge=1)  # 번호대 시작값 (optional)

    _norm_mode = field_validator("numbering_mode")(_validate_numbering_mode)


class StoreGroupResponse(BaseModel):
    """매장 그룹 응답 스키마.

    Store group response schema. duplicate_empids 는 그룹 편성/모드 변경 직후
    채번 스코프 안에 이미 존재하는 중복 empid 목록 (경고용, 블록하지 않음).
    """

    id: str  # 그룹 UUID 문자열 (Group UUID as string)
    organization_id: str  # 소속 조직 UUID 문자열 (Organization UUID as string)
    name: str  # 그룹 이름 (Group name)
    code: str | None = None  # 그룹 코드 (예: "ODG")
    sort_order: int = 0  # 정렬 순서 (Manual display order)
    numbering_mode: str = NUMBERING_MODE_GROUP  # 채번 모드 (group|store)
    number_range_start: int | None = None  # 번호대 시작값 (Range start)
    store_count: int = 0  # 소속 매장 수 (Number of stores in this group)
    duplicate_empids: list[dict[str, int]] = []  # 스코프 내 중복 empid 경고 [{empid, count}]
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


class StoreGroupReorderRequest(BaseModel):
    """매장 그룹 정렬 순서 일괄 변경 요청.

    Bulk store-group reorder request — group IDs in the desired display order.
    """

    # Pydantic 이 UUID 파싱까지 검증 (잘못된 값은 422 — 라우터에서 UUID() 수동 변환 금지)
    group_ids: list[UUID]  # 새 순서의 그룹 UUID 목록 (Group UUIDs in desired order)


# === 그룹 편입 미리보기 (Assign preview) 스키마 ===
# 편입(store.group_id 변경) 전에 공유 채번 스코프의 EMPID 충돌을 미리 보여주는
# 읽기 전용 미리보기. 서버는 편입 시 empid 를 절대 바꾸지 않으므로(정책 A)
# 여기서 경고만 하고, 해소는 Users → Bulk Edit → EMPID 에서 한다.


class GroupAssignPreviewRequest(BaseModel):
    """편입 미리보기 요청 — 매장을 그룹에 넣으면 어떻게 되는지 조회만.

    Assign-preview request: what would happen if store_id joined group_id.
    group_id null = 그룹 이탈 미리보기 (충돌 개념 없음 — 빈 결과).
    """

    store_id: UUID  # 편입할 매장 (Store being assigned — 잘못된 값은 422)
    group_id: UUID | None = None  # 대상 그룹 (null = 이탈 — Leaving a group)


class AssignPreviewMember(BaseModel):
    """미리보기 속 사람 한 명 (편입 매장 쪽) / One person on the incoming side."""

    user_id: str  # 사용자 UUID 문자열 (User UUID as string)
    name: str  # 표시 이름 (Display name)


class AssignPreviewHolder(BaseModel):
    """그룹 내 다른 매장에서 그 번호를 이미 쓰는 사람 / Existing holder of the number."""

    user_id: str  # 사용자 UUID 문자열 (User UUID as string)
    name: str  # 표시 이름 (Display name)
    store_id: str  # 보유 매장 UUID 문자열 (Store where the number is held)
    store_name: str  # 보유 매장 이름 (Store name)


class AssignPreviewConflict(BaseModel):
    """번호 충돌 한 건 — 편입 멤버의 empid 를 그룹 내 다른 사람이 이미 사용 중.

    One conflict: an incoming member's empid is already held by someone else
    in another store of the target group (dormant rows included — numbers
    stay occupied under policy A).
    """

    empid: int  # 충돌 번호 (The colliding EMPID)
    incoming: AssignPreviewMember  # 편입 매장 쪽 보유자 (Incoming member)
    holders: list[AssignPreviewHolder] = []  # 그룹 내 기존 보유자들 (Existing holders)


class AssignPreviewSplitStore(BaseModel):
    """같은 사람이 그룹 내 다른 매장에서 갖는 다른 번호 / The person's other number elsewhere."""

    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
    store_name: str  # 매장 이름 (Store name)
    empid: int  # 그 매장에서의 번호 (EMPID at that store)


class AssignPreviewPersonSplit(BaseModel):
    """같은 사람이 편입 매장과 그룹 내 다른 매장에서 서로 다른 번호를 갖는 케이스.

    Same person, different numbers across the shared scope (same number is
    normal and excluded).
    """

    user_id: str  # 사용자 UUID 문자열 (User UUID as string)
    name: str  # 표시 이름 (Display name)
    incoming_empid: int  # 편입 매장에서의 번호 (EMPID at the incoming store)
    elsewhere: list[AssignPreviewSplitStore] = []  # 그룹 내 다른 매장의 번호들 (Other numbers)


class GroupAssignPreviewResponse(BaseModel):
    """편입 미리보기 응답 — 아무것도 변경하지 않는 조회 결과.

    Assign-preview response (read-only). conflicts/person_splits 는 대상 그룹이
    numbering_mode="group" 일 때만 채워진다 (이탈/독립채번은 충돌 개념 없음).
    """

    numbering_mode: str | None = None  # 대상 그룹 채번 모드 (null = 그룹 이탈)
    conflicts: list[AssignPreviewConflict] = []  # 번호 충돌 목록 (Number conflicts)
    person_splits: list[AssignPreviewPersonSplit] = []  # 인물 분열 목록 (Person splits)
    incoming_with_empid: int = 0  # 편입 매장의 empid 보유 멤버 수 (Members with a number)


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
    group_id: UUID | None = None  # 소속 그룹 UUID (Store group, optional — 잘못된 값은 422)
    number_range_start: int | None = Field(default=None, ge=1)  # 매장 번호대 시작값 (optional)

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
    # 영업시간(operating_hours)은 이 스키마에 없다 — settings registry 키 `store.operating_hours`
    # 로 옮겼고(D2-3), 설정 API(/console/settings)로 저장한다.
    day_start_time: dict[str, str] | None = None  # 영업일 경계 시각 (Day boundary, optional)
    max_work_hours_weekly: int | None = None  # 주간 최대 근무시간 (Max weekly hours, optional)
    state_code: str | None = None  # 주(State) 코드 (US state code, optional)
    timezone: str | None = None  # IANA 타임존 (Store timezone override, optional)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate, optional)
    group_id: UUID | None = None  # 소속 그룹 변경 (null=그룹 해제. 미포함=변경 없음. 잘못된 값은 422)
    number_range_start: int | None = Field(default=None, ge=1)  # 매장 번호대 시작값 (optional)

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
    # 영업시간(operating_hours)은 응답에서 제거됨 — settings registry 키 `store.operating_hours` (D2-3).
    day_start_time: dict[str, str] | None = None  # 영업일 경계 시각 (Day boundary JSONB)
    max_work_hours_weekly: int | None = None  # 주간 최대 근무시간 (Max weekly hours)
    state_code: str | None = None  # 주(State) 코드 (US state code)
    timezone: str | None = None  # IANA 타임존 (Store timezone override)
    default_hourly_rate: float | None = None  # 매장 기본 시급 (Store default hourly rate)
    accepting_signups: bool = True  # 가입/지원 접수 여부 (Hiring signups open flag)
    group_id: str | None = None  # 소속 그룹 UUID (Store group, null=미그룹)
    number_range_start: int | None = None  # 매장 번호대 시작값 (empid range start override)
    duplicate_empids: list[dict[str, int]] = []  # 그룹 편성 직후 스코프 내 중복 경고 [{empid, count}]
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
