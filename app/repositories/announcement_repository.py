"""공지사항 레포지토리 — 공지사항 관련 DB 쿼리 담당.

Announcement Repository — Handles all announcement-related database queries.
Extends BaseRepository with organization-scoped and brand-filtered queries.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Announcement
from app.repositories.base import BaseRepository


class AnnouncementRepository(BaseRepository[Announcement]):
    """공지사항 레포지토리.

    Announcement repository with org-scoped and brand-filtered queries.

    Extends:
        BaseRepository[Announcement]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the announcement repository with Announcement model.
        """
        super().__init__(Announcement)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Announcement], int]:
        """조직 전체 공지사항을 페이지네이션하여 조회합니다.

        Retrieve paginated announcements for an organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Announcement], int]: (공지 목록, 전체 개수)
                                                 (List of announcements, total count)
        """
        query: Select = (
            select(Announcement)
            .where(Announcement.organization_id == organization_id)
            .order_by(Announcement.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)

    async def get_for_user_brands(
        self,
        db: AsyncSession,
        organization_id: UUID,
        brand_ids: list[UUID],
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Announcement], int]:
        """사용자가 속한 브랜드의 공지사항 + 조직 전체 공지를 조회합니다.

        Retrieve announcements for user's brands (org-wide + brand-specific).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            brand_ids: 사용자가 속한 브랜드 UUID 목록 (User's brand UUID list)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Announcement], int]: (공지 목록, 전체 개수)
                                                 (List of announcements, total count)
        """
        # 조직 전체(brand_id=NULL) 또는 사용자 브랜드 소속 공지
        # Org-wide (brand_id is NULL) or user's brand announcements
        query: Select = (
            select(Announcement)
            .where(
                Announcement.organization_id == organization_id,
                or_(
                    Announcement.brand_id.is_(None),
                    Announcement.brand_id.in_(brand_ids),
                ),
            )
            .order_by(Announcement.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)


# 싱글턴 인스턴스 — Singleton instance
announcement_repository: AnnouncementRepository = AnnouncementRepository()
