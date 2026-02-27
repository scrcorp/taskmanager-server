"""체크리스트 항목 리뷰 스키마.

Checklist item review request/response schemas.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ItemReviewUpsert(BaseModel):
    """항목 리뷰 생성/수정 요청 — result만."""

    result: str = Field(..., pattern=r"^(pass|fail|caution)$")


class ReviewContentCreate(BaseModel):
    """리뷰 콘텐츠 추가 요청."""

    type: str = Field(..., pattern=r"^(text|photo|video)$")
    content: str = Field(..., min_length=1)


class ReviewContentResponse(BaseModel):
    """리뷰 콘텐츠 응답."""

    id: str
    review_id: str
    author_id: str
    author_name: str | None = None
    type: str
    content: str
    created_at: datetime


class ItemReviewResponse(BaseModel):
    """항목 리뷰 응답."""

    id: str
    instance_id: str
    item_index: int
    reviewer_id: str
    reviewer_name: str | None = None
    result: str
    contents: list[ReviewContentResponse] = []
    created_at: datetime
    updated_at: datetime
