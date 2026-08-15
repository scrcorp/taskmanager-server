"""연락처(Contacts) Pydantic 스키마 — request/response (v1).

House style: snake_case JSON, id 는 문자열 UUID (다른 도메인 스키마와 동일).
계약: docs/99_inbox/2026-08-14-연락처-API계약.md
설계: docs/99_inbox/2026-08-14-연락처(Contacts)-기능-설계.md (D1~D9)

Schemas:
    - ContactPhoneInput / ContactPhoneResponse: 번호 입력·응답
    - ContactTagResponse: 태그 자동완성 응답 (usage_count 포함)
    - ContactPayload: 연락처 내용 본문 (신청 payload 와 CRUD 가 공유)
    - ContactCreate / ContactUpdate / ContactDeleteRequest: 쓰기 요청
    - ContactResponse / ContactDeleteResponse: 연락처 응답
    - ContactChangeRequestCreate / ...Response: 변경 신청
    - ContactRequestApprove / ContactRequestReject / ContactApproveResponse: 신청 처리
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

__all__ = [
    "MAX_PHONES_PER_CONTACT",
    "MAX_TAGS_PER_CONTACT",
    "MAX_TAG_LENGTH",
    "MAX_SEARCH_LENGTH",
    "ContactPhoneInput",
    "ContactPhoneResponse",
    "ContactTagResponse",
    "ContactTagRef",
    "ContactPayload",
    "ContactCreate",
    "ContactUpdate",
    "ContactDeleteRequest",
    "ContactDuplicatePhone",
    "ContactResponse",
    "ContactDeleteResponse",
    "ContactChangeRequestCreate",
    "ContactChangeRequestResponse",
    "ContactRequestApprove",
    "ContactRequestReject",
    "ContactApproveResponse",
]

# 입력 상한 — 계약 §4.1 / §4.5
MAX_PHONES_PER_CONTACT = 10
MAX_TAGS_PER_CONTACT = 20
MAX_TAG_LENGTH = 40
# 검색어 상한 — 초과분은 라우터/서비스에서 잘라 쓴다(에러 아님, 계약 §4.2.1)
MAX_SEARCH_LENGTH = 100


def _clean_optional_text(v: str | None, field: str, max_len: int) -> str | None:
    """선택 텍스트 필드 정리 — trim 후 빈 문자열은 None, 길이 초과는 에러."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if len(v) > max_len:
        raise ValueError(f"{field} must be {max_len} characters or fewer")
    return v


# === 번호 (D6) ===

class ContactPhoneInput(BaseModel):
    """번호 입력 1건 — 배열 순서가 곧 sort_order (클라이언트는 sort_order 를 보내지 않는다).

    number 는 원본 표기 그대로 저장·표시하고, 정규화 값은 서버가 계산한다
    (app.utils.phone.normalize_phone). 클라이언트가 보내도 무시.
    """

    label: str | None = None  # mobile / office / home / fax / ... 자유 입력
    number: str
    is_primary: bool = False

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Label", 30)

    @field_validator("number")
    @classmethod
    def _check_number(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Phone number is required")
        if len(v) > 50:
            raise ValueError("Phone number must be 50 characters or fewer")
        return v


class ContactPhoneResponse(BaseModel):
    """번호 응답 1건 — number 는 원본 표기, number_normalized 는 서버 계산 값."""

    id: str
    label: str | None
    number: str
    number_normalized: str | None
    is_primary: bool
    sort_order: int


# === 태그 (D7) ===

class ContactTagRef(BaseModel):
    """연락처에 붙은 태그 — 표시명(name) + 정규화 키(key)."""

    id: str
    name: str
    key: str


class ContactTagResponse(BaseModel):
    """태그 자동완성 응답 — GET /contacts/tags.

    usage_count 는 **caller 가 볼 수 있는 연락처 기준** 링크 수(가시성 절 적용).
    """

    id: str
    name: str
    key: str
    usage_count: int


# === 연락처 내용 본문 ===

def _validate_phones(v: list[ContactPhoneInput] | None) -> list[ContactPhoneInput] | None:
    if v is None:
        return None
    if len(v) > MAX_PHONES_PER_CONTACT:
        raise ValueError(f"A contact can have at most {MAX_PHONES_PER_CONTACT} phone numbers")
    if sum(1 for p in v if p.is_primary) > 1:
        raise ValueError("Only one phone number can be marked as primary")
    return v


def _validate_tags(v: list[str] | None) -> list[str] | None:
    """태그 문자열 배열 정리 — trim, 빈 값 제거, 정규화 키 기준 중복 제거(입력 순서 보존)."""
    if v is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in v:
        name = (raw or "").strip()
        if not name:
            continue
        if len(name) > MAX_TAG_LENGTH:
            raise ValueError(f"Each tag must be {MAX_TAG_LENGTH} characters or fewer")
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    if len(cleaned) > MAX_TAGS_PER_CONTACT:
        raise ValueError(f"A contact can have at most {MAX_TAGS_PER_CONTACT} tags")
    return cleaned


class ContactPayload(BaseModel):
    """연락처 내용 본문 — 전체 치환 형태.

    CRUD 요청과 변경 신청 payload 가 같은 형태를 쓴다(계약 §5.2).
    store_id 는 NULL 이면 조직 전체 공유 (D1).
    """

    name: str
    company: str | None = None
    email: str | None = None
    memo: str | None = None
    store_id: str | None = None
    phones: list[ContactPhoneInput] | None = None
    tags: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        if len(v) > 200:
            raise ValueError("Name must be 200 characters or fewer")
        return v

    @field_validator("company")
    @classmethod
    def _check_company(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Company", 200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str | None) -> str | None:
        v = _clean_optional_text(v, "Email", 255)
        # 형식 검증은 느슨하게 — '@' 포함만 요구 (계약 §4.5)
        if v is not None and "@" not in v:
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("memo")
    @classmethod
    def _check_memo(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Memo", 4000)

    @field_validator("phones")
    @classmethod
    def _check_phones(cls, v: list[ContactPhoneInput] | None) -> list[ContactPhoneInput] | None:
        return _validate_phones(v)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str] | None) -> list[str] | None:
        return _validate_tags(v)


# === 쓰기 요청 ===

class ContactCreate(ContactPayload):
    """연락처 생성 요청 — POST /contacts/. reason 은 선택 (D9)."""

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)


class ContactUpdate(BaseModel):
    """연락처 수정 요청 — PUT /contacts/{id}. 전체 치환(PUT semantics).

    키가 없으면 변경 없음, null 이면 NULL 로 설정 (name 만 null 불가).
    서비스는 `model_dump(exclude_unset=True)` 로 "보낸 키"만 반영한다.
    phones / tags 도 동일 — 생략하면 변경 없음, `[]` 이면 전부 삭제.
    reason 은 **필수** (D9).
    """

    name: str | None = None
    company: str | None = None
    email: str | None = None
    memo: str | None = None
    store_id: str | None = None
    phones: list[ContactPhoneInput] | None = None
    tags: list[str] | None = None
    # 필수이지만 타입은 Optional 이다 — 누락을 Pydantic 422 가 아니라 서비스의
    # CONTACT_REASON_REQUIRED(400, 계약 §6)로 내리기 위해서다. 값 검사는 서비스가 한다.
    reason: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        if v is None:
            # 키를 보냈는데 null → name 은 비울 수 없다
            raise ValueError("Name cannot be empty")
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        if len(v) > 200:
            raise ValueError("Name must be 200 characters or fewer")
        return v

    @field_validator("company")
    @classmethod
    def _check_company(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Company", 200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str | None) -> str | None:
        v = _clean_optional_text(v, "Email", 255)
        if v is not None and "@" not in v:
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("memo")
    @classmethod
    def _check_memo(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Memo", 4000)

    @field_validator("phones")
    @classmethod
    def _check_phones(cls, v: list[ContactPhoneInput] | None) -> list[ContactPhoneInput] | None:
        return _validate_phones(v)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str] | None) -> list[str] | None:
        return _validate_tags(v)

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)


class ContactDeleteRequest(BaseModel):
    """연락처 삭제 요청 본문 — DELETE /contacts/{id} (body 있는 DELETE). reason 필수.

    누락·공백은 서비스가 CONTACT_REASON_REQUIRED(400)로 처리한다 (계약 §6). 여기서
    필수로 선언하면 FastAPI 기본 422 로 나가 콘솔이 "사유를 입력하라"는 안내를 못 준다.
    """

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)


# === 연락처 응답 ===

class ContactDuplicatePhone(BaseModel):
    """중복 번호 경고 1건 (N7) — 차단이 아니라 알림.

    같은 정규화 번호를 이미 가진 **다른** 연락처를 알려준다. 저장은 이미 끝난 상태이며
    콘솔은 "Saved. Another contact has the same number." 식으로 안내한다.
    """

    number: str  # 방금 저장한 원본 표기 (Number as entered on this contact)
    number_normalized: str  # 겹친 정규화 값 (Digits-only value that matched)
    contact_id: str  # 같은 번호를 가진 기존 연락처 id
    contact_name: str  # 같은 번호를 가진 기존 연락처 이름


class ContactResponse(BaseModel):
    """연락처 상세/목록 응답.

    store_id 가 NULL 이면 store_name 도 NULL 이며 콘솔은 "All stores" 로 표시한다.
    pending_request_count 는 **상세에서만** 채운다(목록은 N+1 회피로 항상 0).
    duplicate_phone_warnings 는 **생성/수정/승인 응답에만** 채운다(N7 — 경고, 차단 아님).
    """

    id: str
    name: str
    company: str | None
    email: str | None
    memo: str | None
    store_id: str | None
    store_name: str | None
    phones: list[ContactPhoneResponse] = []
    tags: list[ContactTagRef] = []
    created_by: str | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime
    pending_request_count: int = 0
    duplicate_phone_warnings: list[ContactDuplicatePhone] = []


class ContactDeleteResponse(BaseModel):
    """삭제 응답 — 무효화된 pending 신청 수를 함께 알린다(조용한 실패 금지)."""

    message: str
    superseded_request_count: int


# === 변경 신청 (D4) ===

class ContactChangeRequestCreate(BaseModel):
    """변경 신청 생성 — POST /contacts/requests.

    | request_type | contact_id | payload | reason |
    |---|---|---|---|
    | create | null 이어야 함 | 필수 | 선택 |
    | update | 필수 | 필수(전체 치환) | 필수 |
    | delete | 필수 | null 이어야 함 | 필수 |
    """

    request_type: Literal["create", "update", "delete"]
    contact_id: str | None = None
    reason: str | None = None
    payload: ContactPayload | None = None

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)

    # 종류별 shape/사유 검증은 스키마가 아니라 서비스
    # (`contact_service._validate_request_shape`) 에서 한다.
    # Pydantic model_validator 로 두면 FastAPI 기본 422 가 나가는데, 계약 §6 은
    # 이 도메인의 검증 실패를 400 + CONTACT_* 코드로 통일하기 때문이다.
    # (update/delete 의 reason 필수도 같은 이유로 서비스에서 처리한다.)


class ContactChangeRequestResponse(BaseModel):
    """변경 신청 응답.

    contact_name 은 신청 시점 스냅샷이라 대상이 지워져도 남는다.
    is_stale 는 그 사이 원본이 바뀌었는지(N5) — 경고만, 승인은 차단하지 않는다.
    current_contact 는 상세/처리 대기 목록에만 채운다(내 신청 목록은 null).
    """

    id: str
    request_type: str  # 'create' | 'update' | 'delete'
    status: str  # 'pending' | 'approved' | 'rejected' | 'cancelled' | 'superseded'
    contact_id: str | None
    contact_name: str | None
    payload: dict[str, Any] | None
    applied_payload: dict[str, Any] | None = None
    reason: str | None
    requested_by: str | None
    requested_by_name: str | None
    requested_at: datetime
    resolved_by: str | None = None
    resolved_by_name: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    base_updated_at: datetime | None = None
    is_stale: bool = False
    current_contact: ContactResponse | None = None


class ContactRequestApprove(BaseModel):
    """신청 승인 — POST /contacts/requests/{id}/approve. 둘 다 선택.

    payload 를 주면 "수정 후 반영"이며, 신청 원문(payload)은 덮어쓰지 않고
    applied_payload 에 따로 저장한다 (D4 — 신청 원문 영구 보존).
    """

    payload: ContactPayload | None = None
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _check_note(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Note", 500)


class ContactRequestReject(BaseModel):
    """신청 반려 — POST /contacts/requests/{id}/reject. 사유 필수 (N4/D9).

    누락은 서비스가 CONTACT_REASON_REQUIRED(400)로 처리한다 (계약 §6).
    """

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)


class ContactApproveResponse(BaseModel):
    """승인 응답 — 처리된 신청 + 반영 결과 연락처."""

    request: ContactChangeRequestResponse
    contact: ContactResponse
