"""Changelog repository — DB 쿼리 전용 (비즈니스 로직 없음)."""

from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.changelog import ChangelogPost


class ChangelogRepository:
    @staticmethod
    async def get_by_id(db: AsyncSession, post_id: UUID) -> Optional[ChangelogPost]:
        result = await db.execute(select(ChangelogPost).where(ChangelogPost.id == post_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_slug(db: AsyncSession, slug: str) -> Optional[ChangelogPost]:
        result = await db.execute(select(ChangelogPost).where(ChangelogPost.slug == slug))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_published_by_slug(db: AsyncSession, slug: str) -> Optional[ChangelogPost]:
        result = await db.execute(
            select(ChangelogPost).where(
                ChangelogPost.slug == slug,
                ChangelogPost.is_published.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def published_query(category: Optional[str], q: Optional[str]) -> Select:
        """공개 목록 쿼리 — 발행분, 최신순, 카테고리/검색 필터."""
        query = select(ChangelogPost).where(ChangelogPost.is_published.is_(True))
        if category:
            query = query.where(ChangelogPost.category == category)
        if q:
            like = f"%{q}%"
            query = query.where(
                or_(ChangelogPost.title.ilike(like), ChangelogPost.body.ilike(like))
            )
        return query.order_by(ChangelogPost.published_at.desc())

    @staticmethod
    async def list_all(
        db: AsyncSession, category: Optional[str] = None
    ) -> Sequence[ChangelogPost]:
        """백오피스 목록 — 초안 포함, 최신순(생성일)."""
        query = select(ChangelogPost)
        if category:
            query = query.where(ChangelogPost.category == category)
        query = query.order_by(ChangelogPost.created_at.desc())
        result = await db.execute(query)
        return result.scalars().all()


changelog_repository = ChangelogRepository()
