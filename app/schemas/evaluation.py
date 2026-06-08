"""평가 Pydantic 스키마 — Evaluation request/response schemas (v1 redesign).

House style: snake_case JSON, no alias_generator (다른 도메인 스키마와 동일).
JSONB config/template_snapshot shape 의 서브모델(CriterionConfig/ScalePoint/
EvalTemplateConfig)은 app/core/evaluation.py 에서 재사용한다.

Schemas:
    - EvalTemplateResponse: 평가 템플릿 응답 (조직 Basic 1개, read-only)
    - EvaluationCreate / EvaluationUpdate: 평가 작성/수정 요청
    - EvaluationResponse: 평가 상세/목록 응답 (joined names + average)
    - EvaluatableUserResponse: 평가 가능 직원 picker 응답
"""

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

# config/template_snapshot JSONB 서브모델 재사용 — single source of truth.
from app.core.evaluation import CriterionConfig, EvalTemplateConfig, ScalePoint

__all__ = [
    "CriterionConfig",
    "ScalePoint",
    "TemplateConfig",
    "EvalTemplateResponse",
    "EvaluationCreate",
    "EvaluationUpdate",
    "EvaluationResponse",
    "StoreRef",
    "EvaluatableUserResponse",
    "EvaluatableUsersPage",
]

# TemplateConfig = config JSONB 의 응답 형태(= EvalTemplateConfig). 이름만 contract 에 맞춤.
TemplateConfig = EvalTemplateConfig


# === 평가 템플릿 (Eval Template) 응답 ===

class EvalTemplateResponse(BaseModel):
    """평가 템플릿 응답 스키마 — GET /templates, /templates/{id}.

    v1 은 조직당 빌트인 Basic 1개만 노출(read-only).
    """

    id: str
    name: str
    is_default: bool
    status: str  # 'published'
    version: int
    config: TemplateConfig
    created_at: datetime
    updated_at: datetime


# === 평가 (Evaluation) 요청 ===

class EvaluationCreate(BaseModel):
    """평가 생성 요청 스키마 — POST /.

    required: evaluatee_id 만 (draft 부분 저장 허용).
    store_id / period_start / period_end / responses 는 draft 에서 optional.
    status='submitted' 면 store_id + 기간(비미래) + submit-gate(9개 전부) 를
    service 에서 강제한다. 기간 규칙은 §M5 — 미래 금지 + start<=end.
    """

    evaluatee_id: str  # 피평가자 UUID
    store_id: str | None = None  # 대상 매장 UUID (draft optional, submit 필수)
    position_id: str | None = None  # 대상 포지션 UUID
    period_start: date | None = None  # 평가 기간 시작 (draft optional)
    period_end: date | None = None  # 평가 기간 종료 (draft optional)
    responses: dict[str, int] = {}  # {criterion_code: 1..5}
    improvement: str | None = None
    good_examples: str | None = None
    status: Literal["draft", "submitted"] = "draft"

    @model_validator(mode="after")
    def _check_period(self) -> "EvaluationCreate":
        """기간 정합성 — 둘 다 있으면 start<=end, 있으면 미래 금지 (else 422).

        draft 라도 입력된 날짜는 미래일 수 없다(§M5). 둘 다 None 인 draft 는 통과.
        """
        today = datetime.now(timezone.utc).date()
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_end < self.period_start
        ):
            raise ValueError("period_end must be on or after period_start")
        if self.period_start is not None and self.period_start > today:
            raise ValueError("period_start cannot be in the future")
        if self.period_end is not None and self.period_end > today:
            raise ValueError("period_end cannot be in the future")
        return self

    @field_validator("responses")
    @classmethod
    def _check_scores(cls, v: dict[str, int]) -> dict[str, int]:
        """각 점수는 int 이고 1..5 범위여야 함 (else 422). 미지의 code 검증은 service."""
        for code, score in v.items():
            if not isinstance(score, int) or isinstance(score, bool):
                raise ValueError(f"Score for '{code}' must be an integer")
            if not (1 <= score <= 5):
                raise ValueError(f"Score for '{code}' must be between 1 and 5")
        return v


class EvaluationUpdate(BaseModel):
    """평가 수정 요청 스키마 — PUT /{id}. 모든 필드 optional (partial update).

    제공된 필드만 반영. status='submitted' 로 전환 시 submit-gate 강제.
    draft / submitted 양쪽에서 수정 가능 (mockup "Update").
    """

    evaluatee_id: str | None = None
    store_id: str | None = None
    position_id: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    responses: dict[str, int] | None = None
    improvement: str | None = None
    good_examples: str | None = None
    status: Literal["draft", "submitted"] | None = None

    @model_validator(mode="after")
    def _check_period(self) -> "EvaluationUpdate":
        """둘 다 제공된 경우에만 start<=end; 제공된 날짜는 미래 금지 (§M5)."""
        today = datetime.now(timezone.utc).date()
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_end < self.period_start
        ):
            raise ValueError("period_end must be on or after period_start")
        if self.period_start is not None and self.period_start > today:
            raise ValueError("period_start cannot be in the future")
        if self.period_end is not None and self.period_end > today:
            raise ValueError("period_end cannot be in the future")
        return self

    @field_validator("responses")
    @classmethod
    def _check_scores(cls, v: dict[str, int] | None) -> dict[str, int] | None:
        """각 점수는 int 이고 1..5 범위여야 함 (else 422)."""
        if v is None:
            return v
        for code, score in v.items():
            if not isinstance(score, int) or isinstance(score, bool):
                raise ValueError(f"Score for '{code}' must be an integer")
            if not (1 <= score <= 5):
                raise ValueError(f"Score for '{code}' must be between 1 and 5")
        return v


# === 평가 (Evaluation) 응답 ===

class EvaluationResponse(BaseModel):
    """평가 상세/목록 응답 스키마 — GET /, GET /{id}, POST, PUT.

    snapshot-resolved 이름(evaluatee_name 등)과 항상 계산되는 average 포함.
    """

    id: str
    status: str  # 'draft' | 'submitted'
    evaluatee_id: str | None
    evaluatee_name: str | None  # users.full_name (read 시점 resolve)
    employee_no: str | None  # users.employee_no (live read; None → UI "—")
    evaluator_id: str | None
    evaluator_name: str | None
    store_id: str | None
    store_name: str | None
    position_id: str | None
    position_name: str | None  # live position name (job_title 과 다를 수 있음)
    job_title: str | None  # 작성 시점 스냅샷
    period_start: date | None  # draft 는 NULL 가능
    period_end: date | None  # draft 는 NULL 가능
    template_id: str | None
    template_snapshot: TemplateConfig  # 채점 기준(9 criteria + scale)
    responses: dict[str, int]
    average: float | None  # rated 항목 평균(1-dp), 없으면 None
    improvement: str | None
    good_examples: str | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None


# === 평가 가능 직원 (Evaluatable User) 응답 ===

class StoreRef(BaseModel):
    """매장 참조 — id + name 만 (picker dropdown 용)."""

    id: str
    name: str


class EvaluatableUserResponse(BaseModel):
    """평가 가능 직원 응답 스키마 — GET /evaluatable-users.

    평가자보다 엄격히 낮은 권한(더 큰 priority)인 활성 직원만.
    store_* / position_* 는 후보의 primary store(가장 먼저 배정된 user_stores)
    기준 prefill 값. position 은 user_stores 에 컬럼이 없어 best-effort/None.
    stores: 후보가 배정된 모든 매장(org-scope) — picker Store dropdown 제한용(§M1).
    """

    id: str
    full_name: str
    employee_no: str | None
    role_name: str
    role_priority: int
    store_id: str | None  # primary store (prefill)
    store_name: str | None
    position_id: str | None  # primary store 의 position (prefill), None 가능
    position_name: str | None
    stores: list[StoreRef]  # 후보의 모든 매장 (§M1 dropdown 제한)


class EvaluatableUsersPage(BaseModel):
    """평가 가능 직원 페이지 응답 — GET /evaluatable-users (paginated envelope).

    무한 스크롤 + 서버 검색(q) 을 위한 page-based pagination (§P1).
    """

    items: list[EvaluatableUserResponse]
    total: int
    page: int
    limit: int
    has_more: bool
