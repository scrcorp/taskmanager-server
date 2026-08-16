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

from app.models.contact import CONTACT_TARGET_TYPES, CONTACT_VISIBILITIES

__all__ = [
    "MAX_PHONES_PER_CONTACT",
    "MAX_TAGS_PER_CONTACT",
    "MAX_TAG_LENGTH",
    "MAX_TARGETS_PER_CONTACT",
    "MAX_SEARCH_LENGTH",
    "ContactVisibility",
    "ContactTargetRef",
    "ContactTargetInput",
    "ContactVisibilityPreview",
    "ContactBulkCreate",
    "ContactBulkRowResult",
    "ContactBulkCreateResult",
    "ContactBulkUpdate",
    "ContactBulkUpdateResult",
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
# 공개 대상 상한 — 조직 규모를 넘길 이유가 없다. 방어적 상한.
MAX_TARGETS_PER_CONTACT = 200
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


# === 가시성 (확장 D1) ===

class ContactTargetRef(BaseModel):
    """공개 대상 한 건 (응답) — 타입 + id + 표시명."""

    type: str  # CONTACT_TARGET_TYPES
    id: str
    name: str


class ContactTargetInput(BaseModel):
    """공개 대상 한 건 (요청)."""

    type: str
    id: str

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in CONTACT_TARGET_TYPES:
            raise ValueError(f"target type must be one of {CONTACT_TARGET_TYPES}")
        return v


def _validate_visibility(v: str | None) -> str | None:
    """가시성 모드 값 검사 — 모델 상수(CONTACT_VISIBILITIES)가 단일 원천."""
    if v is None:
        return None
    v = v.strip()
    if v not in CONTACT_VISIBILITIES:
        raise ValueError(f"visibility must be one of {CONTACT_VISIBILITIES}")
    return v


def _validate_targets(
    v: list[ContactTargetInput] | None,
) -> list[ContactTargetInput] | None:
    """대상 목록 정리 — 중복 제거(입력 순서 보존), 상한 검사.

    org 소속·접근 권한 검사는 서비스가 한다(도메인 에러 코드로 내리기 위해).
    """
    if v is None:
        return None
    cleaned: list[ContactTargetInput] = []
    seen: set[tuple[str, str]] = set()
    for t in v:
        key = (t.type, (t.id or "").strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
    if len(cleaned) > MAX_TARGETS_PER_CONTACT:
        raise ValueError(
            f"A contact can target at most {MAX_TARGETS_PER_CONTACT} entries"
        )
    return cleaned


def _validate_user_ids(v: list[str] | None) -> list[str] | None:
    """제외자 id 목록 정리 — trim, 빈 값·중복 제거."""
    if v is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in v:
        uid = (raw or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        cleaned.append(uid)
    return cleaned


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
    가시성은 **명시 모드**다 — 대상이 비었다고 전체 공유가 되지 않는다 (V1).
    모드 ↔ 목록의 정합성 검증은 서비스가 한다(도메인 에러 코드 400 으로 내리기 위해).
    """

    name: str
    company: str | None = None
    email: str | None = None
    memo: str | None = None
    visibility: str = "organization"
    targets: list[ContactTargetInput] | None = None
    excluded_user_ids: list[str] | None = None
    phones: list[ContactPhoneInput] | None = None
    tags: list[str] | None = None

    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, v: str) -> str:
        checked = _validate_visibility(v)
        assert checked is not None  # 필수 필드라 None 이 올 수 없다
        return checked

    @field_validator("targets")
    @classmethod
    def _check_targets(
        cls, v: list[ContactTargetInput] | None
    ) -> list[ContactTargetInput] | None:
        return _validate_targets(v)

    @field_validator("excluded_user_ids")
    @classmethod
    def _check_excluded(cls, v: list[str] | None) -> list[str] | None:
        return _validate_user_ids(v)

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


class ContactVisibilityPreview(BaseModel):
    """가시성 미리보기 요청 — 저장 전에 "지금 누가 보는가"를 묻는다 (V4/V5)."""

    visibility: str = "organization"
    targets: list[ContactTargetInput] | None = None
    excluded_user_ids: list[str] | None = None

    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, v: str) -> str:
        checked = _validate_visibility(v)
        assert checked is not None
        return checked

    @field_validator("targets")
    @classmethod
    def _check_targets(
        cls, v: list[ContactTargetInput] | None
    ) -> list[ContactTargetInput] | None:
        return _validate_targets(v)

    @field_validator("excluded_user_ids")
    @classmethod
    def _check_excluded(cls, v: list[str] | None) -> list[str] | None:
        return _validate_user_ids(v)


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
    visibility: str | None = None
    targets: list[ContactTargetInput] | None = None
    excluded_user_ids: list[str] | None = None
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

    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, v: str | None) -> str | None:
        if v is None:
            # 키를 보냈는데 null → 가시성은 비울 수 없다
            raise ValueError("Visibility cannot be empty")
        return _validate_visibility(v)

    @field_validator("targets")
    @classmethod
    def _check_targets(
        cls, v: list[ContactTargetInput] | None
    ) -> list[ContactTargetInput] | None:
        # null 은 "대상 없음"으로 읽는다 (전체 공유 전환 시 콘솔이 그렇게 보낸다)
        return _validate_targets(v) if v is not None else []

    @field_validator("excluded_user_ids")
    @classmethod
    def _check_excluded(cls, v: list[str] | None) -> list[str] | None:
        return _validate_user_ids(v) if v is not None else []

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

    visibility='organization' 이면 targets 는 빈 배열이며 콘솔은 "All stores" 로 표시한다.
    pending_request_count 는 **상세에서만** 채운다(목록은 N+1 회피로 항상 0).
    duplicate_phone_warnings 는 **생성/수정/승인 응답에만** 채운다(N7 — 경고, 차단 아님).
    """

    id: str
    name: str
    company: str | None
    email: str | None
    memo: str | None
    visibility: str
    targets: list[ContactTargetRef] = []
    excluded_users: list[ContactTargetRef] = []
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


# === 대량 등록 / 일괄 수정 (D1~D6) ===

# 한 번에 다룰 수 있는 최대 건수 — 미리보기를 사람이 눈으로 훑을 수 있는 규모로 묶는다.
MAX_BULK_ROWS = 200


class ContactBulkCreate(BaseModel):
    """대량 등록 — 붙여넣기 표에서 파싱된 행들 (D3).

    `dry_run=True` 면 저장하지 않고 검증 결과만 돌려준다. 화면은 항상 먼저 dry_run 을
    부르고, 사용자가 확인한 뒤에 실제 저장을 부른다 (D4: 전부 취소 + 미리보기).
    """

    rows: list[ContactPayload]
    reason: str | None = None
    dry_run: bool = True

    @field_validator("rows")
    @classmethod
    def _check_rows(cls, v: list[ContactPayload]) -> list[ContactPayload]:
        if len(v) == 0:
            raise ValueError("Add at least one row")
        if len(v) > MAX_BULK_ROWS:
            raise ValueError(f"Up to {MAX_BULK_ROWS} rows at a time")
        return v

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)


class ContactBulkRowResult(BaseModel):
    """대량 등록 미리보기의 한 행 결과.

    `valid=False` 면 `error` 에 사람이 읽을 이유가 담긴다. 하나라도 실패하면 전체가
    저장되지 않는다 — 어느 줄이 문제인지 화면이 짚어줄 수 있게 index 를 함께 준다.

    **이름이 `ok` 가 아니라 `valid` 인 이유**: 이건 요청의 성공/실패가 아니라 **행의 검증
    결과**다. 요청 자체는 200 으로 성공했고, 실패는 상태코드로만 알린다는 규약(G7 레칫)을
    깨지 않기 위해 성공 플래그처럼 읽히는 이름을 쓰지 않는다.
    """

    index: int
    name: str
    valid: bool
    error: str | None = None
    # 같은 번호를 이미 가진 연락처 경고 (차단 아님, N7/D6)
    duplicate_phone_warnings: list[ContactDuplicatePhone] = []


class ContactBulkCreateResult(BaseModel):
    """대량 등록 결과 — dry_run 이면 `created` 는 0 이고 rows 만 채워진다."""

    dry_run: bool
    total: int
    valid_count: int
    failed_count: int
    created: int
    batch_id: str | None = None
    rows: list[ContactBulkRowResult] = []


class ContactBulkUpdate(BaseModel):
    """일괄 수정 (D2).

    v1 이 다루는 것: 태그 추가 / 태그 제거 / 회사명 설정 / 가시성 설정.
    **memo 는 일부러 뺐다** — 일괄 덮어쓰기는 기존 메모를 전부 날리는데,
    그 사고 위험이 얻는 편의보다 크다.
    """

    contact_ids: list[str]
    add_tags: list[str] | None = None
    remove_tags: list[str] | None = None
    company: str | None = None
    visibility: str | None = None
    targets: list[ContactTargetInput] | None = None
    excluded_user_ids: list[str] | None = None
    # 필수지만 Optional 타입 — 누락을 도메인 400 으로 내리기 위해(다른 쓰기 경로와 동일)
    reason: str | None = None
    dry_run: bool = True

    @field_validator("contact_ids")
    @classmethod
    def _check_ids(cls, v: list[str]) -> list[str]:
        cleaned = [x.strip() for x in v if (x or "").strip()]
        if not cleaned:
            raise ValueError("Select at least one contact")
        if len(cleaned) > MAX_BULK_ROWS:
            raise ValueError(f"Up to {MAX_BULK_ROWS} contacts at a time")
        return cleaned

    @field_validator("add_tags", "remove_tags")
    @classmethod
    def _check_tags(cls, v: list[str] | None) -> list[str] | None:
        return _validate_tags(v)

    @field_validator("company")
    @classmethod
    def _check_company(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Company", 200)

    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, v: str | None) -> str | None:
        return _validate_visibility(v)

    @field_validator("targets")
    @classmethod
    def _check_targets(
        cls, v: list[ContactTargetInput] | None
    ) -> list[ContactTargetInput] | None:
        return _validate_targets(v)

    @field_validator("excluded_user_ids")
    @classmethod
    def _check_excluded(cls, v: list[str] | None) -> list[str] | None:
        return _validate_user_ids(v)

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, "Reason", 500)


class ContactBulkUpdateResult(BaseModel):
    """일괄 수정 결과.

    `changed` 는 실제로 값이 바뀐 건수다 — 이미 그 상태였던 건은 이력을 남기지 않으므로
    선택 건수와 다를 수 있다(계약 §4.6 no-op 규칙).
    """

    dry_run: bool
    selected: int
    changed: int
    batch_id: str | None = None


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
