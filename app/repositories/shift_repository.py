"""근무조 레포지토리 — 근무조 CRUD 쿼리.

Shift Repository — CRUD queries for shifts.
Extends BaseRepository with Shift-specific database operations.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work import Shift
from app.repositories.base import BaseRepository


class ShiftRepository(BaseRepository[Shift]):
    """근무조 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the shifts table.
    Provides brand-scoped shift retrieval.
    """

    def __init__(self) -> None:
        """ShiftRepository를 초기화합니다.

        Initialize the ShiftRepository with the Shift model.
        """
        super().__init__(Shift)

    async def get_by_brand(
        self,
        db: AsyncSession,
        brand_id: UUID,
    ) -> list[Shift]:
        """브랜드에 속한 모든 근무조를 정렬 순서로 조회합니다.

        Retrieve all shifts belonging to a brand, ordered by sort_order.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)

        Returns:
            list[Shift]: 근무조 목록 (List of shifts ordered by sort_order)
        """
        query: Select = (
            select(Shift)
            .where(Shift.brand_id == brand_id)
            .order_by(Shift.sort_order)
        )
        result = await db.execute(query)
        return list(result.scalars().all())


# 싱글턴 인스턴스 — Singleton instance
shift_repository: ShiftRepository = ShiftRepository()
