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
    "SignatureInfo",
    "WarningSignaturesResponse",
    "WarningSignRequest",
    "SavedSignatureResponse",
    "SavedSignatureUpdate",
    "StoreRef",
    "WarnableUserResponse",
    "WarnableUsersPage",
    "WarningCountItem",
]

# 벡터 서명 입력 상한 — 악의/사고성 거대 페이로드 방어.
MAX_STROKES = 500
MAX_POINTS = 10000


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
    # 서명 방식 — 'digital'(기본) | 'wet'. 종이 운영 매장은 발행 시 wet 선택.
    signature_method: Literal["digital", "wet"] = "digital"

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
    # 직원 확인(읽음) 일시 — 직원이 앱에서 상세를 처음 열면 자동 stamp (NULL=미확인).
    # 확인 != 서명: signatures 와 독립.
    acknowledged_at: datetime | None = None
    # party 별 적용 서명 — {"employee": SigInfo|None, "manager": SigInfo|None}.
    # 미서명 party 는 None. SigInfo 는 적용 순간 박제된 벡터 스냅샷.
    signatures: dict[str, "SignatureInfo | None"] = {}
    # === wet 서명 ===
    # 'digital'(앱/콘솔 벡터) | 'wet'(출력→실물 서명→PDF 업로드).
    signature_method: str = "digital"
    # 스토어 코드 — 파일명/표시용 (stores.code). subject 매장 기준.
    store_code: str | None = None
    # wet PDF 업로드 여부 + 메타 (실제 파일은 별도 다운로드 엔드포인트로 인증 후 제공).
    signed_pdf_present: bool = False
    wet_signed_on: date | None = None
    wet_uploaded_at: datetime | None = None
    # 서명완료 파생 bool — 콘솔/앱/배지가 모두 이 단일 값을 소비.
    # digital: warning_signatures 행 유무. wet: signed_pdf_key 유무(양쪽 갈음).
    employee_signed: bool = False
    manager_signed: bool = False
    created_at: datetime
    updated_at: datetime


# === 서명 ===


class SignatureInfo(BaseModel):
    """적용된 서명 1개 정보 — WarningResponse.signatures[party] 값.

    signature_strokes 는 적용 순간 박제된 벡터 스냅샷(유저의 현재 저장 서명과 무관).
    """

    signer_user_id: str | None
    signer_name: str | None
    # 캡처 계정 — 온-디바이스(콘솔)에서 받은 경우 실제 조작 계정. 명의와 같으면(셀프사인)
    # 서버가 None 으로 내려준다(표시 불필요).
    captured_by_user_id: str | None = None
    captured_by_name: str | None = None
    signed_at: datetime
    method: str  # 'drawn' | 'saved'
    signature_strokes: dict  # {"strokes":[[[x,y]..]..],"aspect":w/h}


class WarningSignaturesResponse(BaseModel):
    """party 별 서명 묶음 — {"employee": SigInfo|None, "manager": SigInfo|None}."""

    employee: SignatureInfo | None = None
    manager: SignatureInfo | None = None


def _validate_strokes(strokes: list[list[list[float]]]) -> list[list[list[float]]]:
    """벡터 스트로크 검증 — 비어있지 않고, 0..1 정규화, 상한 이내.

    - strokes 비어있으면 거부 (실제 서명 강제)
    - 각 point 는 [x, y] (정확히 2 좌표), 0.0 ≤ x,y ≤ 1.0
    - strokes 수 ≤ MAX_STROKES, 총 point 수 ≤ MAX_POINTS
    """
    if not isinstance(strokes, list) or not strokes:
        raise ValueError("Signature strokes cannot be empty")
    if len(strokes) > MAX_STROKES:
        raise ValueError(f"Too many strokes (max {MAX_STROKES})")
    total_points = 0
    for stroke in strokes:
        if not isinstance(stroke, list) or not stroke:
            raise ValueError("Each stroke must be a non-empty list of points")
        total_points += len(stroke)
        if total_points > MAX_POINTS:
            raise ValueError(f"Too many points (max {MAX_POINTS})")
        for pt in stroke:
            if not isinstance(pt, list) or len(pt) != 2:
                raise ValueError("Each point must be [x, y]")
            x, y = pt
            if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
                raise ValueError("Point coordinates must be numbers")
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("Point coordinates must be normalized to 0..1")
    return strokes


class WarningSignRequest(BaseModel):
    """경고 서명 요청 — POST /{id}/sign.

    strokes 는 정규화(0..1) 벡터. method='saved' 면 저장 서명에서 적용한 것(감사용),
    'drawn' 이면 새로 그린 것. save_as_default=True 면 이 서명을 users.signature_strokes
    로도 저장(재사용 템플릿 갱신, 본인 셀프사인일 때만 서버가 반영).

    party: 콘솔 온-디바이스 사인에서 어느 서명란인지('employee'|'manager'). 앱 셀프사인
    엔드포인트는 이 값을 무시하고 항상 employee 로 강제한다(앱 유저가 매니저란 서명 불가).
    """

    strokes: list[list[list[float]]]
    aspect: float | None = None
    method: Literal["drawn", "saved"] = "drawn"
    save_as_default: bool = False
    party: Literal["employee", "manager"] = "manager"

    @field_validator("strokes")
    @classmethod
    def _check_strokes(cls, v: list[list[list[float]]]) -> list[list[list[float]]]:
        return _validate_strokes(v)

    def to_strokes_payload(self) -> dict:
        """DB 저장용 스냅샷 dict — {"strokes": ..., "aspect": ...}."""
        return {"strokes": self.strokes, "aspect": self.aspect}


class WarningMethodSwitchRequest(BaseModel):
    """서명 방식 전환 요청 — PUT /{id}/method.

    digital↔wet 양방향. 기존 서명/PDF 가 있으면 무효화되고 재서명 대기로 리셋된다
    (service 에서 처리, 재서명 알림 발송). 전환은 철회가 아님(status 유지).
    """

    method: Literal["digital", "wet"]


class SavedSignatureResponse(BaseModel):
    """저장 서명 조회 응답 — {"signature": {strokes, aspect} | None}.

    signature=None 이면 아직 저장 서명 없음.
    """

    signature: dict | None = None


class SavedSignatureUpdate(BaseModel):
    """저장 서명 설정 요청 — {strokes, aspect}. users.signature_strokes 갱신."""

    strokes: list[list[list[float]]]
    aspect: float | None = None

    @field_validator("strokes")
    @classmethod
    def _check_strokes(cls, v: list[list[list[float]]]) -> list[list[list[float]]]:
        return _validate_strokes(v)

    def to_strokes_payload(self) -> dict:
        return {"strokes": self.strokes, "aspect": self.aspect}


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


# WarningResponse.signatures 가 SignatureInfo 를 forward-ref 하므로 rebuild.
WarningResponse.model_rebuild()
