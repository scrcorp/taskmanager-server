"""Changelog 비즈니스 로직 — slug 생성/중복 회피, 발행 토글, CRUD.

글로벌(org 밖) 데이터. 트랜잭션 commit 은 호출측(라우터)에서.
"""

import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.changelog import ChangelogPost
from app.repositories.changelog_repository import changelog_repository
from app.schemas.changelog import ChangelogCreate, ChangelogUpdate
from app.services.storage_service import storage_service


def _slugify(value: str) -> str:
    """제목/슬러그 → URL-safe slug (소문자, 영숫자+하이픈, 최대 120)."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:120] or "post"


# 마크다운 이미지 패턴: ![alt](url)
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def body_urls_to_keys(md: str) -> str:
    """본문 마크다운의 이미지 URL을 버킷 상대경로(key)로 치환.

    버킷/S3 URL(extract_key가 key를 돌려주는 것)만 key로 바꾼다.
    외부/비버킷 URL은 그대로 둔다. DB 저장 직전에 적용 (절대 URL 저장 금지).
    """
    if not md:
        return md

    def _sub(m: re.Match[str]) -> str:
        alt, url = m.group(1), m.group(2)
        if not url.startswith("http"):
            return m.group(0)  # 이미 상대경로
        key = storage_service.extract_key(url)
        if not key or key.startswith("http"):
            return m.group(0)  # 비버킷 외부 URL — 유지
        return f"![{alt}]({key})"

    return _IMG_RE.sub(_sub, md)


def body_keys_to_urls(md: str) -> str:
    """본문 마크다운의 상대경로(key) 이미지를 전체 URL로 치환.

    http로 시작하지 않는(=상대경로 key) 이미지만 resolve_url로 변환.
    이미 절대 URL인 항목은 그대로 둔다. 읽기 시점(공개 상세/편집 로드)에 적용.
    """
    if not md:
        return md

    def _sub(m: re.Match[str]) -> str:
        alt, url = m.group(1), m.group(2)
        if url.startswith("http"):
            return m.group(0)  # 이미 절대 URL
        resolved = storage_service.resolve_url(url)
        if not resolved:
            return m.group(0)
        return f"![{alt}]({resolved})"

    return _IMG_RE.sub(_sub, md)


class ChangelogError(Exception):
    """changelog 도메인 에러 (라우터에서 4xx 로 변환)."""


class ChangelogNotFound(ChangelogError):
    pass


class ChangelogService:
    async def _unique_slug(self, db: AsyncSession, base: str, exclude_id: UUID | None = None) -> str:
        """전역 고유 slug 보장 — 충돌 시 -2, -3 … suffix."""
        candidate = base
        n = 1
        while True:
            existing = await changelog_repository.get_by_slug(db, candidate)
            if existing is None or existing.id == exclude_id:
                return candidate
            n += 1
            suffix = f"-{n}"
            candidate = f"{base[: 120 - len(suffix)]}{suffix}"

    async def create(self, db: AsyncSession, payload: ChangelogCreate) -> ChangelogPost:
        base = _slugify(payload.slug or payload.title)
        slug = await self._unique_slug(db, base)
        post = ChangelogPost(
            slug=slug,
            category=payload.category,
            title=payload.title,
            summary=payload.summary,
            body=body_urls_to_keys(payload.body),
            cover_image_key=payload.cover_image_key,
            tags=payload.tags,
            is_published=False,
        )
        db.add(post)
        await db.flush()
        return post

    async def update(
        self, db: AsyncSession, post_id: UUID, payload: ChangelogUpdate
    ) -> ChangelogPost:
        post = await changelog_repository.get_by_id(db, post_id)
        if post is None:
            raise ChangelogNotFound("Changelog post not found")
        data = payload.model_dump(exclude_unset=True)
        if "body" in data and data["body"] is not None:
            data["body"] = body_urls_to_keys(data["body"])
        for field in ("title", "category", "body", "summary", "tags", "cover_image_key"):
            if field in data:
                setattr(post, field, data[field])
        await db.flush()
        return post

    async def set_published(
        self, db: AsyncSession, post_id: UUID, published: bool
    ) -> ChangelogPost:
        post = await changelog_repository.get_by_id(db, post_id)
        if post is None:
            raise ChangelogNotFound("Changelog post not found")
        post.is_published = published
        # 최초 발행 시 timestamp 설정. 발행 취소 시 유지(이력) — 재발행해도 첫 발행일 보존하려면
        # 여기서 None 으로 두지 않는다. 단순화: 발행 시 항상 현재시각으로 갱신.
        if published:
            post.published_at = datetime.now(timezone.utc)
        await db.flush()
        return post

    async def delete(self, db: AsyncSession, post_id: UUID) -> None:
        post = await changelog_repository.get_by_id(db, post_id)
        if post is None:
            raise ChangelogNotFound("Changelog post not found")
        await db.delete(post)
        await db.flush()


changelog_service = ChangelogService()
