"""브랜드 레포지토리 — 브랜드 CRUD 및 관련 쿼리.

Brand Repository — CRUD and related queries for brands.
Extends BaseRepository with Brand-specific database operations
including shift/position eager loading.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Brand
from app.repositories.base import BaseRepository


class BrandRepository(BaseRepository[Brand]):
    """브랜드 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the brands table.
    Provides organization-scoped brand retrieval and detail loading.
    """

    def __init__(self) -> None:
        """BrandRepository를 초기화합니다.

        Initialize the BrandRepository with the Brand model.
        """
        super().__init__(Brand)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[Brand]:
        """조직에 속한 모든 브랜드를 조회합니다.

        Retrieve all brands belonging to a specific organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[Brand]: 브랜드 목록 (List of brands)
        """
        query: Select = (
            select(Brand)
            .where(Brand.organization_id == organization_id)
            .order_by(Brand.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_detail(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> Brand | None:
        """브랜드 상세 정보를 근무조/직책과 함께 조회합니다.

        Retrieve brand detail with shifts and positions eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 범위 필터 (Organization scope filter)

        Returns:
            Brand | None: 근무조/직책이 로드된 브랜드 또는 None
                          (Brand with shifts/positions loaded, or None)
        """
        query: Select = (
            select(Brand)
            .options(selectinload(Brand.shifts), selectinload(Brand.positions))
            .where(Brand.id == brand_id, Brand.organization_id == organization_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


# 싱글턴 인스턴스 — Singleton instance
brand_repository: BrandRepository = BrandRepository()
