"""직책 레포지토리 — 직책 CRUD 쿼리.

Position Repository — CRUD queries for positions.
Extends BaseRepository with Position-specific database operations.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.work import Position
from app.repositories.base import BaseRepository


class PositionRepository(BaseRepository[Position]):
    """직책 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the positions table.
    Provides store-scoped position retrieval.
    """

    def __init__(self) -> None:
        """PositionRepository를 초기화합니다.

        Initialize the PositionRepository with the Position model.
        """
        super().__init__(Position)

    async def get_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
    ) -> list[Position]:
        """매장에 속한 모든 직책을 정렬 순서로 조회합니다.

        Retrieve all positions belonging to a store, ordered by sort_order.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)

        Returns:
            list[Position]: 직책 목록 (List of positions ordered by sort_order)
        """
        query: Select = (
            select(Position)
            .where(Position.store_id == store_id)
            .order_by(Position.sort_order)
        )
        result = await db.execute(query)
        return list(result.scalars().all())


# 싱글턴 인스턴스 — Singleton instance
position_repository: PositionRepository = PositionRepository()
