"""직책 서비스 — 직책 CRUD 비즈니스 로직.

Position Service — Business logic for position CRUD operations.
Handles creation, retrieval, update, and deletion of positions
under a specific brand with organization scope verification.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Brand
from app.models.work import Position
from app.repositories.brand_repository import brand_repository
from app.repositories.position_repository import position_repository
from app.schemas.work import PositionCreate, PositionResponse, PositionUpdate
from app.utils.exceptions import DuplicateError, NotFoundError


class PositionService:
    """직책 관련 비즈니스 로직을 처리하는 서비스.

    Service handling position business logic.
    Provides CRUD operations for positions under a brand with org scope verification.
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
            brand_id=str(position.brand_id),
            name=position.name,
            sort_order=position.sort_order,
        )

    async def _verify_brand_ownership(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> Brand:
        """브랜드가 조직에 속하는지 확인합니다.

        Verify that the brand belongs to the given organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            Brand: 확인된 브랜드 (Verified brand)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없거나 조직에 속하지 않을 때
                           (Brand not found or not in organization)
        """
        brand: Brand | None = await brand_repository.get_by_id(
            db, brand_id, organization_id
        )
        if brand is None:
            raise NotFoundError("Brand not found")
        return brand

    async def list_positions(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> list[PositionResponse]:
        """브랜드에 속한 직책 목록을 조회합니다.

        List all positions belonging to a brand.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[PositionResponse]: 직책 목록 (List of position responses)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)
        positions: list[Position] = await position_repository.get_by_brand(
            db, brand_id
        )
        return [self._to_response(p) for p in positions]

    async def create_position(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
        data: PositionCreate,
    ) -> PositionResponse:
        """새 직책을 생성합니다.

        Create a new position under a brand.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 직책 생성 데이터 (Position creation data)

        Returns:
            PositionResponse: 생성된 직책 응답 (Created position response)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
            DuplicateError: 같은 이름의 직책이 이미 존재할 때
                            (Position with same name already exists)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)

        # 이름 중복 확인 — Check name uniqueness within brand
        exists: bool = await position_repository.exists(
            db, {"brand_id": brand_id, "name": data.name}
        )
        if exists:
            raise DuplicateError(
                "A position with this name already exists in this brand"
            )

        position: Position = await position_repository.create(
            db,
            {
                "brand_id": brand_id,
                "name": data.name,
                "sort_order": data.sort_order,
            },
        )
        return self._to_response(position)

    async def update_position(
        self,
        db: AsyncSession,
        position_id: UUID,
        brand_id: UUID,
        organization_id: UUID,
        data: PositionUpdate,
    ) -> PositionResponse:
        """직책 정보를 수정합니다.

        Update an existing position.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            position_id: 직책 ID (Position UUID)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            PositionResponse: 수정된 직책 응답 (Updated position response)

        Raises:
            NotFoundError: 직책을 찾을 수 없을 때 (Position not found)
            DuplicateError: 같은 이름의 직책이 이미 존재할 때
                            (Position with same name already exists)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)

        # 기존 직책 확인 — Verify position exists under this brand
        existing: Position | None = await position_repository.get_by_id(
            db, position_id
        )
        if existing is None or existing.brand_id != brand_id:
            raise NotFoundError("Position not found in this brand")

        # 이름 변경 시 중복 확인 — Check name uniqueness if changing name
        if data.name is not None and data.name != existing.name:
            name_exists: bool = await position_repository.exists(
                db, {"brand_id": brand_id, "name": data.name}
            )
            if name_exists:
                raise DuplicateError(
                    "A position with this name already exists in this brand"
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
        brand_id: UUID,
        organization_id: UUID,
    ) -> None:
        """직책을 삭제합니다.

        Delete a position by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            position_id: 직책 ID (Position UUID)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 직책을 찾을 수 없을 때 (Position not found)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)

        # 직책이 이 브랜드에 속하는지 확인 — Verify position belongs to this brand
        existing: Position | None = await position_repository.get_by_id(
            db, position_id
        )
        if existing is None or existing.brand_id != brand_id:
            raise NotFoundError("Position not found in this brand")

        deleted: bool = await position_repository.delete(db, position_id)
        if not deleted:
            raise NotFoundError("Position not found")


# 싱글턴 인스턴스 — Singleton instance
position_service: PositionService = PositionService()
