"""직책 서비스 — 직책 CRUD 비즈니스 로직.

Position Service — Business logic for position CRUD operations.
Handles creation, retrieval, update, and deletion of positions
under a specific store with organization scope verification.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store
from app.models.work import Position
from app.repositories.store_repository import store_repository
from app.repositories.position_repository import position_repository
from app.schemas.work import PositionCreate, PositionResponse, PositionUpdate
from app.utils.exceptions import DuplicateError, NotFoundError


class PositionService:
    """직책 관련 비즈니스 로직을 처리하는 서비스.

    Service handling position business logic.
    Provides CRUD operations for positions under a store with org scope verification.
    """

    def _to_response(self, position: Position) -> PositionResponse:
        """직책 모델을 응답 스키마로 변환합니다.

        Convert a Position model instance to a PositionResponse schema.

        Args:
            position: 직책 모델 (Position model instance)

        Returns:
            PositionResponse: 직책 응답 (Position response)
        """
        return PositionResponse(
            id=str(position.id),
            store_id=str(position.store_id),
            name=position.name,
            sort_order=position.sort_order,
        )

    async def _verify_store_ownership(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> Store:
        """매장이 조직에 속하는지 확인합니다.

        Verify that the store belongs to the given organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            Store: 확인된 매장 (Verified store)

        Raises:
            NotFoundError: 매장을 찾을 수 없거나 조직에 속하지 않을 때
                           (Store not found or not in organization)
        """
        store: Store | None = await store_repository.get_by_id(
            db, store_id, organization_id
        )
        if store is None:
            raise NotFoundError("Store not found")
        return store

    async def list_positions(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> list[PositionResponse]:
        """매장에 속한 직책 목록을 조회합니다.

        List all positions belonging to a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[PositionResponse]: 직책 목록 (List of position responses)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
        """
        await self._verify_store_ownership(db, store_id, organization_id)
        positions: list[Position] = await position_repository.get_by_store(
            db, store_id
        )
        return [self._to_response(p) for p in positions]

    async def create_position(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: PositionCreate,
    ) -> PositionResponse:
        """새 직책을 생성합니다.

        Create a new position under a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 직책 생성 데이터 (Position creation data)

        Returns:
            PositionResponse: 생성된 직책 응답 (Created position response)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
            DuplicateError: 같은 이름의 직책이 이미 존재할 때
                            (Position with same name already exists)
        """
        await self._verify_store_ownership(db, store_id, organization_id)

        # 이름 중복 확인 — Check name uniqueness within store
        exists: bool = await position_repository.exists(
            db, {"store_id": store_id, "name": data.name}
        )
        if exists:
            raise DuplicateError(
                "A position with this name already exists in this store"
            )

        # sort_order 자동 계산 — 항상 맨 마지막에 추가
        existing_positions: list[Position] = await position_repository.get_by_store(
            db, store_id
        )
        next_order: int = max((p.sort_order for p in existing_positions), default=-1) + 1

        position: Position = await position_repository.create(
            db,
            {
                "store_id": store_id,
                "name": data.name,
                "sort_order": next_order,
            },
        )
        return self._to_response(position)

    async def update_position(
        self,
        db: AsyncSession,
        position_id: UUID,
        store_id: UUID,
        organization_id: UUID,
        data: PositionUpdate,
    ) -> PositionResponse:
        """직책 정보를 수정합니다.

        Update an existing position.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            position_id: 직책 ID (Position UUID)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            PositionResponse: 수정된 직책 응답 (Updated position response)

        Raises:
            NotFoundError: 직책을 찾을 수 없을 때 (Position not found)
            DuplicateError: 같은 이름의 직책이 이미 존재할 때
                            (Position with same name already exists)
        """
        await self._verify_store_ownership(db, store_id, organization_id)

        # 기존 직책 확인 — Verify position exists under this store
        existing: Position | None = await position_repository.get_by_id(
            db, position_id
        )
        if existing is None or existing.store_id != store_id:
            raise NotFoundError("Position not found in this store")

        # 이름 변경 시 중복 확인 — Check name uniqueness if changing name
        if data.name is not None and data.name != existing.name:
            name_exists: bool = await position_repository.exists(
                db, {"store_id": store_id, "name": data.name}
            )
            if name_exists:
                raise DuplicateError(
                    "A position with this name already exists in this store"
                )

        update_data: dict = data.model_dump(exclude_unset=True)
        position: Position | None = await position_repository.update(
            db, position_id, update_data
        )
        if position is None:
            raise NotFoundError("Position not found")

        return self._to_response(position)

    async def delete_position(
        self,
        db: AsyncSession,
        position_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """직책을 삭제합니다.

        Delete a position by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            position_id: 직책 ID (Position UUID)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 직책을 찾을 수 없을 때 (Position not found)
        """
        await self._verify_store_ownership(db, store_id, organization_id)

        # 직책이 이 매장에 속하는지 확인 — Verify position belongs to this store
        existing: Position | None = await position_repository.get_by_id(
            db, position_id
        )
        if existing is None or existing.store_id != store_id:
            raise NotFoundError("Position not found in this store")

        deleted: bool = await position_repository.delete(db, position_id)
        if not deleted:
            raise NotFoundError("Position not found")


# 싱글턴 인스턴스 — Singleton instance
position_service: PositionService = PositionService()
