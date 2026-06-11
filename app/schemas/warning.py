"""경고 Pydantic 스키마 — Warning request/response schemas (v1).

House style: snake_case JSON, no alias_generator (다른 도메인 스키마와 동일).
사유 카테고리 코드 검증은 app/core/warning.WARNING_CATEGORY_CODES 를 단일 원천으로 쓴다.

Schemas:
    - WarningCreate / WarningUpdate: 경고 발행/수정 요청
    - WarningResponse: 경고 상세/목록 응답 (joined names + ref_no)
    - WarnableUserResponse / WarnableUsersPage: 경고 대상 직원 picker
    - WarningCountItem: 직원별 경고 갯수 (Staff 목록 칼럼용)
"""

from datetime import date, datetime, time, timezone
from typing import Literal

from pydantic import BaseModel, field_validator

__all__ = [
    "WarningCreate",
    "WarningUpdate",
    "WarningResponse",
    "StoreRef",
    "WarnableUserResponse",
    "WarnableUsersPage",
    "WarningCountItem",
]


def _validate_categories(v: list[str]) -> list[str]:
    """비어있지 않고 중복 제거(입력 순서 보존).

    코드 유효성(org 카테고리 존재/비삭제)은 서비스가 검증한다
    (app.services.warning_category_service.validate_codes — 수정 시 legacy 코드 허용).
    """
    if not v:
        raise ValueError("At least one reason category is required")
    seen: set[str] = set()
    deduped: list[str] = []
    for c in v:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _not_future(d: date) -> date:
    if d > datetime.now(timezone.utc).date():
        raise ValueError("Warning date cannot be in the future")
    return d


# === 요청 ===

class WarningCreate(BaseModel):
    """경고 발행 요청 — POST /.

    subject_user_id / store_id / title / categories / warning_date 필수.
    store_id 는 대상 직원이 소속된 매장 중 하나여야 한다(service 검증).
    """

    subject_user_id: str
    store_id: str
    title: str
    categories: list[str]
    details: str | None = None
    corrective_action: str | None = None
    other_text: str | None = None  # 'other' 카테고리 체크 시 자유텍스트
    deadline: date | None = None  # 시정 마감일
    follow_up_date: date | None = None  # 후속 미팅 날짜
    follow_up_time: time | None = None  # 후속 시간 (None=미정/TBD)
    # 발행자(매니저) override — Owner 만 다른 매니저 대신 발행 가능 (service 강제)
    issued_by_id: str | None = None
    warning_date: date

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Title is required")
        return v

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, v: list[str]) -> list[str]:
        return _validate_categories(v)

    @field_validator("warning_date")
    @classmethod
    def _check_date(cls, v: date) -> date:
        return _not_future(v)


class WarningUpdate(BaseModel):
    """경고 수정 요청 — PUT /{id}. 모든 필드 optional (partial update).

    대상 직원(subject)은 변경 불가(발행 후 고정). store/제목/사유/상세/상태/일자만.
    status='withdrawn'|'active' 로 철회/복구 토글 (철회는 기록 유지).
    """

    store_id: str | None = None
    title: str | None = None
    categories: list[str] | None = None
    details: str | None = None
    corrective_action: str | None = None
    other_text: str | None = None
    deadline: date | None = None
    follow_up_date: date | None = None
    follow_up_time: time | None = None
    issued_by_id: str | None = None  # Owner 만 발행자 변경 가능 (service 강제)
    status: Literal["active", "withdrawn"] | None = None
    warning_date: date | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Title cannot be blank")
        return v

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _validate_categories(v)

    @field_validator("warning_date")
    @classmethod
    def _check_date(cls, v: date | None) -> date | None:
        if v is None:
            return v
        return _not_future(v)


# === 응답 ===

class WarningResponse(BaseModel):
    """경고 상세/목록 응답 — GET /, GET /{id}, POST, PUT.

    ref_no 는 "W-{seq:05d}" 표시용 사람 ID. 이름들은 read 시점 resolve.
    """

    id: str
    ref_no: str  # "W-00046"
    status: str  # 'active' | 'withdrawn'
    subject_user_id: str | None
    subject_name: str | None  # users.full_name (read 시점 resolve)
    employee_no: str | None
    issued_by_id: str | None
    issued_by_name: str | None
    store_id: str | None
    store_name: str | None
    title: str
    categories: list[str]
    # code → label 맵 (live resolve, 삭제된 legacy 코드 포함). 프론트가 라벨 표시 +
    # active 목록과 비교해 '(removed)' legacy 판별.
    category_labels: dict[str, str]
    details: str | None
    corrective_action: str | None
    other_text: str | None
    deadline: date | None
    follow_up_date: date | None
    follow_up_time: time | None
    warning_date: date
    # 그 직원의 경고 순번 (1=First, 2=Second, ≥3=Other) — 상세에서만 채워짐.
    ordinal: int | None = None
    withdrawn_at: datetime | None
    created_at: datetime
    updated_at: datetime


# === 경고 대상 직원 (picker) ===

class StoreRef(BaseModel):
    """매장 참조 — id + name 만 (picker dropdown 용)."""

    id: str
    name: str


class WarnableUserResponse(BaseModel):
    """경고 대상 직원 응답 — GET /warnable-users.

    발행자보다 엄격히 낮은 권한(더 큰 priority)인 활성 직원만.
    store_* 는 primary store(가장 먼저 배정된 user_stores) prefill.
    stores: 후보가 배정된 모든 매장(org-scope) — picker Store dropdown 제한용.
    """

    id: str
    full_name: str
    employee_no: str | None
    role_name: str
    role_priority: int
    store_id: str | None  # primary store (prefill)
    store_name: str | None
    stores: list[StoreRef]  # 후보의 모든 매장


class WarnableUsersPage(BaseModel):
    """경고 대상 직원 페이지 응답 — GET /warnable-users (paginated envelope)."""

    items: list[WarnableUserResponse]
    total: int
    page: int
    limit: int
    has_more: bool


# === 직원별 경고 갯수 (Staff 목록 칼럼용) ===

class WarningCountItem(BaseModel):
    """직원 1명의 경고 집계 — GET /counts.

    Staff 목록의 Warnings 칼럼(갯수만)용. active = 미해결 경고 수.
    """

    user_id: str
    total: int
    active: int
