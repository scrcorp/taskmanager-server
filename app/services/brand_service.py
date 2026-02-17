"""브랜드 서비스 — 브랜드 CRUD 비즈니스 로직.

Brand Service — Business logic for brand CRUD operations.
Handles creation, retrieval, update, and deletion of brands
within an organization scope.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Brand
from app.repositories.brand_repository import brand_repository
from app.schemas.organization import (
    BrandCreate,
    BrandDetailResponse,
    BrandResponse,
    BrandUpdate,
    PositionResponse,
    ShiftResponse,
)
from app.utils.exceptions import DuplicateError, NotFoundError


class BrandService:
    """브랜드 관련 비즈니스 로직을 처리하는 서비스.

    Service handling brand business logic.
    Provides CRUD operations scoped to the current organization.
    """

    def _to_response(self, brand: Brand) -> BrandResponse:
        """브랜드 모델을 응답 스키마로 변환합니다.

        Convert a Brand model instance to a BrandResponse schema.

        Args:
            brand: 브랜드 모델 (Brand model instance)

        Returns:
            BrandResponse: 브랜드 응답 (Brand response)
        """
        return BrandResponse(
            id=str(brand.id),
            organization_id=str(brand.organization_id),
            name=brand.name,
            address=brand.address,
            is_active=brand.is_active,
            created_at=brand.created_at,
        )

    async def list_brands(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[BrandResponse]:
        """조직에 속한 브랜드 목록을 조회합니다.

        List all brands belonging to the organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[BrandResponse]: 브랜드 목록 (List of brand responses)
        """
        brands: list[Brand] = await brand_repository.get_by_org(db, organization_id)
        return [self._to_response(b) for b in brands]

    async def get_brand(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> BrandDetailResponse:
        """브랜드 상세 정보를 근무조/직책과 함께 조회합니다.

        Retrieve brand detail with shifts and positions.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            BrandDetailResponse: 브랜드 상세 응답 (Brand detail response)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
        """
        brand: Brand | None = await brand_repository.get_detail(
            db, brand_id, organization_id
        )
        if brand is None:
            raise NotFoundError("Brand not found")

        return BrandDetailResponse(
            id=str(brand.id),
            organization_id=str(brand.organization_id),
            name=brand.name,
            address=brand.address,
            is_active=brand.is_active,
            created_at=brand.created_at,
            shifts=[
                ShiftResponse(id=str(s.id), name=s.name, sort_order=s.sort_order)
                for s in brand.shifts
            ],
            positions=[
                PositionResponse(id=str(p.id), name=p.name, sort_order=p.sort_order)
                for p in brand.positions
            ],
        )

    async def create_brand(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: BrandCreate,
    ) -> BrandResponse:
        """새 브랜드를 생성합니다.

        Create a new brand within an organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 소속 조직 ID (Parent organization UUID)
            data: 브랜드 생성 데이터 (Brand creation data)

        Returns:
            BrandResponse: 생성된 브랜드 응답 (Created brand response)

        Raises:
            DuplicateError: 같은 이름의 브랜드가 이미 존재할 때
                            (When a brand with the same name already exists)
        """
        # 같은 조직 내 브랜드명 중복 확인 — Check brand name uniqueness within org
        exists: bool = await brand_repository.exists(
            db, {"organization_id": organization_id, "name": data.name}
        )
        if exists:
            raise DuplicateError("A brand with this name already exists")

        brand: Brand = await brand_repository.create(
            db,
            {
                "organization_id": organization_id,
                "name": data.name,
                "address": data.address,
            },
        )
        return self._to_response(brand)

    async def update_brand(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
        data: BrandUpdate,
    ) -> BrandResponse:
        """브랜드 정보를 수정합니다.

        Update an existing brand.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            BrandResponse: 수정된 브랜드 응답 (Updated brand response)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
            DuplicateError: 같은 이름의 브랜드가 이미 존재할 때
                            (When a brand with the same name already exists)
        """
        # 이름 변경 시 중복 확인 — Check name uniqueness if changing name
        if data.name is not None:
            existing: Brand | None = await brand_repository.get_by_id(
                db, brand_id, organization_id
            )
            if existing is not None and existing.name != data.name:
                name_exists: bool = await brand_repository.exists(
                    db, {"organization_id": organization_id, "name": data.name}
                )
                if name_exists:
                    raise DuplicateError("A brand with this name already exists")

        update_data: dict = data.model_dump(exclude_unset=True)
        brand: Brand | None = await brand_repository.update(
            db, brand_id, update_data, organization_id
        )
        if brand is None:
            raise NotFoundError("Brand not found")

        return self._to_response(brand)

    async def delete_brand(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> None:
        """브랜드를 삭제합니다.

        Delete a brand by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
        """
        deleted: bool = await brand_repository.delete(db, brand_id, organization_id)
        if not deleted:
            raise NotFoundError("Brand not found")


# 싱글턴 인스턴스 — Singleton instance
brand_service: BrandService = BrandService()
