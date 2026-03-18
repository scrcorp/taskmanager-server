"""체크리스트 항목 리뷰 스키마.

Checklist item review request/response schemas.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ItemReviewUpsert(BaseModel):
    """항목 리뷰 생성/수정 요청 — result + 선택적 인라인 코멘트."""

    result: str = Field(..., pattern=r"^(pass|fail|pending_re_review)$")
    comment_text: str | None = None
    comment_photo_url: str | None = None


class ReviewContentCreate(BaseModel):
    """리뷰 콘텐츠 추가 요청."""

    type: str = Field(..., pattern=r"^(text|photo|video)$")
    content: str = Field(..., min_length=1)


class ReviewContentResponse(BaseModel):
    """리뷰 콘텐츠 응답."""

    id: str
    review_id: str | None = None
    author_id: str
    author_name: str | None = None
    type: str
    content: str
    created_at: datetime


class ReviewHistoryItem(BaseModel):
    """리뷰 결과 변경 히스토리 항목."""

    id: str
    changed_by: str
    changed_by_name: str | None = None
    old_result: str | None = None
    new_result: str
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
    history: list[ReviewHistoryItem] = []
    created_at: datetime
    updated_at: datetime


class ResubmitRequest(BaseModel):
    """Staff 재제출 요청."""

    photo_urls: list[str] | None = None
    # backward compat: single photo_url is wrapped into list in service
    photo_url: str | None = None
    note: str | None = None
    location: dict | None = None
    client_timezone: str | None = None


class CompletionHistoryResponse(BaseModel):
    """완료 히스토리 (재제출 아카이브) 응답."""

    id: str
    photo_urls: list[str] | None = None
    note: str | None = None
    location: dict | None = None
    submitted_at: datetime
    created_at: datetime


class ScoreUpdate(BaseModel):
    """인스턴스 점수 부여/수정 요청."""

    score: int = Field(..., ge=0, le=100)
    score_note: str | None = None


class ScoreResponse(BaseModel):
    """인스턴스 점수 응답."""

    score: int | None = None
    score_note: str | None = None
    scored_by: str | None = None
    scored_at: datetime | None = None


class BulkReviewRequest(BaseModel):
    """아이템 일괄 리뷰 요청 — 여러 item_index를 한 번에 pass 처리."""

    item_indexes: list[int] = Field(..., min_length=1)
    result: str = Field("pass", pattern=r"^pass$")
