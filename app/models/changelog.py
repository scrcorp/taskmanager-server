"""Changelog (제품 업데이트 내역) 모델.

플랫폼 전체 공통(글로벌) 업데이트 내역. org 권한 밖 — 운영자(백오피스)가 작성/발행,
사용자는 console/app/homepage 에서 읽기 전용 조회. 따라서 organization_id 가 없다.

- category: 제품 표면(staff_app/attendance_app/console/homepage)
- body: 마크다운 텍스트 (WYSIWYG 입력 → markdown 저장)
- cover_image_key: 상대경로 key 만 저장, 응답 시 storage_service.resolve_url 로 변환
- tags: 자유 태그 배열 (feature/bugfix/... 등)
- is_published + published_at: 발행 상태. 공개 조회는 published 만 노출.

SoT: docs/99_inbox/2026-06-29 changelog-공개-업데이트내역-설계.md
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 카테고리 — 입력 검증은 Pydantic Literal(schemas)에서. DB는 String + 코드값.
# general = 제품 전반/통합 업데이트(특정 표면에 한정되지 않음). 나머지는 제품 표면별.
CHANGELOG_CATEGORIES: tuple[str, ...] = (
    "general",
    "staff_app",
    "attendance_app",
    "console",
    "homepage",
)


class ChangelogPost(Base):
    """글로벌 changelog 게시글 (제품 업데이트 내역)."""

    __tablename__ = "changelog_posts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 전역 고유 slug (URL 식별자). 발행/초안 무관 단일 행.
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # 카테고리: general | staff_app | attendance_app | console | homepage
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 목록/카드용 한 줄 요약 (옵션)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 본문 마크다운
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 커버 이미지 — 상대경로 key (resolve_url 로 런타임 변환). 절대 URL 저장 금지.
    cover_image_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 자유 태그 배열
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        # 공개 목록 쿼리 — 카테고리별 최신순(발행분만 인덱싱).
        Index(
            "ix_changelog_category_published_at",
            "category",
            "published_at",
            postgresql_where=text("is_published IS TRUE"),
        ),
    )
