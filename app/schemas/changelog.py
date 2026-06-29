"""Changelog Pydantic 스키마.

- ChangelogCreate/Update: 백오피스 작성/수정 입력
- ChangelogAdminResponse: 백오피스용(초안 포함 전체 필드)
- ChangelogListItem / ChangelogDetail: 공개 조회 응답 (목록은 body 제외, 상세는 포함)

category 검증은 Literal 로 edge 에서. body 는 마크다운 텍스트.
"""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

ChangelogCategory = Literal["staff_app", "attendance_app", "console", "homepage"]


class ChangelogCreate(BaseModel):
    """백오피스 게시글 생성 입력. slug 미지정 시 title 에서 생성."""

    title: str = Field(..., min_length=1, max_length=200)
    category: ChangelogCategory
    body: str = Field(..., min_length=1)
    summary: Optional[str] = Field(None, max_length=500)
    slug: Optional[str] = Field(None, max_length=120)
    tags: list[str] = Field(default_factory=list)
    cover_image_key: Optional[str] = Field(None, max_length=500)


class ChangelogUpdate(BaseModel):
    """부분 수정. 전달된 필드만 반영."""

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    category: Optional[ChangelogCategory] = None
    body: Optional[str] = Field(None, min_length=1)
    summary: Optional[str] = Field(None, max_length=500)
    tags: Optional[list[str]] = None
    cover_image_key: Optional[str] = Field(None, max_length=500)


class ChangelogAdminResponse(BaseModel):
    """백오피스 응답 — 초안 포함 전체."""

    id: UUID
    slug: str
    category: str
    title: str
    summary: Optional[str]
    body: str
    cover_image_url: Optional[str]  # resolve_url 변환 결과
    tags: list[str]
    is_published: bool
    published_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class ChangelogListItem(BaseModel):
    """공개 목록 항목 — body 제외(페이로드 절감)."""

    slug: str
    category: str
    title: str
    summary: Optional[str]
    cover_image_url: Optional[str]
    tags: list[str]
    published_at: datetime


class ChangelogDetail(BaseModel):
    """공개 상세 — body 포함."""

    slug: str
    category: str
    title: str
    summary: Optional[str]
    body: str
    cover_image_url: Optional[str]
    tags: list[str]
    published_at: datetime
