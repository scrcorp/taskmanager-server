"""평가 Pydantic 스키마 — Evaluation request/response schemas.

Evaluation Pydantic schema definitions.
Includes schemas for eval templates, template items, evaluations, and responses.
"""

from datetime import datetime
from pydantic import BaseModel


# === 평가 템플릿 (Eval Template) 스키마 ===

class EvalTemplateItemCreate(BaseModel):
    """평가 항목 생성 스키마."""
    title: str
    type: str = "score"  # score, text
    max_score: int = 5
    sort_order: int = 0


class EvalTemplateItemResponse(BaseModel):
    """평가 항목 응답 스키마."""
    id: str
    title: str
    type: str
    max_score: int
    sort_order: int


class EvalTemplateCreate(BaseModel):
    """평가 템플릿 생성 스키마."""
    name: str
    target_role: str | None = None
    eval_type: str = "adhoc"  # adhoc, regular
    cycle_weeks: int | None = None
    items: list[EvalTemplateItemCreate] = []


class EvalTemplateUpdate(BaseModel):
    """평가 템플릿 수정 스키마."""
    name: str | None = None
    target_role: str | None = None
    eval_type: str | None = None
    cycle_weeks: int | None = None
    items: list[EvalTemplateItemCreate] | None = None


class EvalTemplateResponse(BaseModel):
    """평가 템플릿 응답 스키마."""
    id: str
    name: str
    target_role: str | None = None
    eval_type: str
    cycle_weeks: int | None = None
    item_count: int = 0
    items: list[EvalTemplateItemResponse] = []
    created_at: datetime
    updated_at: datetime


# === 평가 (Evaluation) 스키마 ===

class EvalResponseCreate(BaseModel):
    """평가 응답 (개별 항목) 생성 스키마."""
    template_item_id: str
    score: int | None = None
    text: str | None = None


class EvalResponseOut(BaseModel):
    """평가 응답 (개별 항목) 응답 스키마."""
    id: str
    template_item_id: str
    item_title: str | None = None
    score: int | None = None
    text: str | None = None


class EvaluationCreate(BaseModel):
    """평가 생성 스키마."""
    evaluatee_id: str
    template_id: str
    store_id: str | None = None
    responses: list[EvalResponseCreate] = []


class EvaluationResponse(BaseModel):
    """평가 응답 스키마."""
    id: str
    evaluator_id: str
    evaluator_name: str | None = None
    evaluatee_id: str
    evaluatee_name: str | None = None
    template_id: str | None = None
    template_name: str | None = None
    store_id: str | None = None
    store_name: str | None = None
    status: str
    responses: list[EvalResponseOut] = []
    created_at: datetime
    submitted_at: datetime | None = None
