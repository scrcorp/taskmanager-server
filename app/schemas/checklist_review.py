"""체크리스트 항목 리뷰 스키마.

Checklist item review request/response schemas.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ItemReviewUpsert(BaseModel):
    """항목 리뷰 생성/수정 요청."""

    result: str = Field(..., pattern=r"^(pass|fail|caution)$")
    comment: str | None = None
    photo_url: str | None = None


class ItemReviewResponse(BaseModel):
    """항목 리뷰 응답."""

    id: str
    instance_id: str
    item_index: int
    reviewer_id: str
    reviewer_name: str | None = None
    result: str
    comment: str | None
    photo_url: str | None
    created_at: datetime
    updated_at: datetime
