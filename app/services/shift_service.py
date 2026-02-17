"""근무조 서비스 — 근무조 CRUD 비즈니스 로직.

Shift Service — Business logic for shift CRUD operations.
Handles creation, retrieval, update, and deletion of shifts
under a specific brand with organization scope verification.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Brand
from app.models.work import Shift
from app.repositories.brand_repository import brand_repository
from app.repositories.shift_repository import shift_repository
from app.schemas.work import ShiftCreate, ShiftResponse, ShiftUpdate
from app.utils.exceptions import DuplicateError, NotFoundError


class ShiftService:
    """근무조 관련 비즈니스 로직을 처리하는 서비스.

    Service handling shift business logic.
    Provides CRUD operations for shifts under a brand with org scope verification.
    """

    def _to_response(self, shift: Shift) -> ShiftResponse:
        """근무조 모델을 응답 스키마로 변환합니다.

        Convert a Shift model instance to a ShiftResponse schema.

        Args:
            shift: 근무조 모델 (Shift model instance)

        Returns:
            ShiftResponse: 근무조 응답 (Shift response)
        """
        return ShiftResponse(
            id=str(shift.id),
            brand_id=str(shift.brand_id),
            name=shift.name,
            sort_order=shift.sort_order,
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

    async def list_shifts(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> list[ShiftResponse]:
        """브랜드에 속한 근무조 목록을 조회합니다.

        List all shifts belonging to a brand.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[ShiftResponse]: 근무조 목록 (List of shift responses)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)
        shifts: list[Shift] = await shift_repository.get_by_brand(db, brand_id)
        return [self._to_response(s) for s in shifts]

    async def create_shift(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
        data: ShiftCreate,
    ) -> ShiftResponse:
        """새 근무조를 생성합니다.

        Create a new shift under a brand.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 근무조 생성 데이터 (Shift creation data)

        Returns:
            ShiftResponse: 생성된 근무조 응답 (Created shift response)

        Raises:
            NotFoundError: 브랜드를 찾을 수 없을 때 (Brand not found)
            DuplicateError: 같은 이름의 근무조가 이미 존재할 때
                            (Shift with same name already exists)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)

        # 이름 중복 확인 — Check name uniqueness within brand
        exists: bool = await shift_repository.exists(
            db, {"brand_id": brand_id, "name": data.name}
        )
        if exists:
            raise DuplicateError("A shift with this name already exists in this brand")

        shift: Shift = await shift_repository.create(
            db,
            {
                "brand_id": brand_id,
                "name": data.name,
                "sort_order": data.sort_order,
            },
        )
        return self._to_response(shift)

    async def update_shift(
        self,
        db: AsyncSession,
        shift_id: UUID,
        brand_id: UUID,
        organization_id: UUID,
        data: ShiftUpdate,
    ) -> ShiftResponse:
        """근무조 정보를 수정합니다.

        Update an existing shift.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            shift_id: 근무조 ID (Shift UUID)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            ShiftResponse: 수정된 근무조 응답 (Updated shift response)

        Raises:
            NotFoundError: 근무조를 찾을 수 없을 때 (Shift not found)
            DuplicateError: 같은 이름의 근무조가 이미 존재할 때
                            (Shift with same name already exists)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)

        # 기존 근무조 확인 — Verify shift exists under this brand
        existing: Shift | None = await shift_repository.get_by_id(db, shift_id)
        if existing is None or existing.brand_id != brand_id:
            raise NotFoundError("Shift not found in this brand")

        # 이름 변경 시 중복 확인 — Check name uniqueness if changing name
        if data.name is not None and data.name != existing.name:
            name_exists: bool = await shift_repository.exists(
                db, {"brand_id": brand_id, "name": data.name}
            )
            if name_exists:
                raise DuplicateError(
                    "A shift with this name already exists in this brand"
                )

        update_data: dict = data.model_dump(exclude_unset=True)
        shift: Shift | None = await shift_repository.update(db, shift_id, update_data)
        if shift is None:
            raise NotFoundError("Shift not found")

        return self._to_response(shift)

    async def delete_shift(
        self,
        db: AsyncSession,
        shift_id: UUID,
        brand_id: UUID,
        organization_id: UUID,
    ) -> None:
        """근무조를 삭제합니다.

        Delete a shift by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            shift_id: 근무조 ID (Shift UUID)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 근무조를 찾을 수 없을 때 (Shift not found)
        """
        await self._verify_brand_ownership(db, brand_id, organization_id)

        # 근무조가 이 브랜드에 속하는지 확인 — Verify shift belongs to this brand
        existing: Shift | None = await shift_repository.get_by_id(db, shift_id)
        if existing is None or existing.brand_id != brand_id:
            raise NotFoundError("Shift not found in this brand")

        deleted: bool = await shift_repository.delete(db, shift_id)
        if not deleted:
            raise NotFoundError("Shift not found")


# 싱글턴 인스턴스 — Singleton instance
shift_service: ShiftService = ShiftService()
