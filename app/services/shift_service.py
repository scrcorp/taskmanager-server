"""근무조 서비스 — 근무조 CRUD 비즈니스 로직.

Shift Service — Business logic for shift CRUD operations.
Handles creation, retrieval, update, and deletion of shifts
under a specific store with organization scope verification.
"""

import re
from uuid import UUID

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checklist import ChecklistTemplate
from app.models.organization import Store
from app.models.work import Position, Shift
from app.repositories.store_repository import store_repository
from app.repositories.shift_repository import shift_repository
from app.schemas.work import ShiftCreate, ShiftResponse, ShiftUpdate
from app.utils.exceptions import DuplicateError, NotFoundError


class ShiftService:
    """근무조 관련 비즈니스 로직을 처리하는 서비스.

    Service handling shift business logic.
    Provides CRUD operations for shifts under a store with org scope verification.
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
            store_id=str(shift.store_id),
            name=shift.name,
            sort_order=shift.sort_order,
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

    async def list_shifts(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> list[ShiftResponse]:
        """매장에 속한 근무조 목록을 조회합니다.

        List all shifts belonging to a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[ShiftResponse]: 근무조 목록 (List of shift responses)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
        """
        await self._verify_store_ownership(db, store_id, organization_id)
        shifts: list[Shift] = await shift_repository.get_by_store(db, store_id)
        return [self._to_response(s) for s in shifts]

    async def create_shift(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: ShiftCreate,
    ) -> ShiftResponse:
        """새 근무조를 생성합니다.

        Create a new shift under a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 근무조 생성 데이터 (Shift creation data)

        Returns:
            ShiftResponse: 생성된 근무조 응답 (Created shift response)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
            DuplicateError: 같은 이름의 근무조가 이미 존재할 때
                            (Shift with same name already exists)
        """
        await self._verify_store_ownership(db, store_id, organization_id)

        # 이름 중복 확인 — Check name uniqueness within store
        exists: bool = await shift_repository.exists(
            db, {"store_id": store_id, "name": data.name}
        )
        if exists:
            raise DuplicateError("A shift with this name already exists in this store")

        shift: Shift = await shift_repository.create(
            db,
            {
                "store_id": store_id,
                "name": data.name,
                "sort_order": data.sort_order,
            },
        )
        return self._to_response(shift)

    async def update_shift(
        self,
        db: AsyncSession,
        shift_id: UUID,
        store_id: UUID,
        organization_id: UUID,
        data: ShiftUpdate,
    ) -> ShiftResponse:
        """근무조 정보를 수정합니다.

        Update an existing shift.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            shift_id: 근무조 ID (Shift UUID)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            ShiftResponse: 수정된 근무조 응답 (Updated shift response)

        Raises:
            NotFoundError: 근무조를 찾을 수 없을 때 (Shift not found)
            DuplicateError: 같은 이름의 근무조가 이미 존재할 때
                            (Shift with same name already exists)
        """
        store: Store = await self._verify_store_ownership(db, store_id, organization_id)

        # 기존 근무조 확인 — Verify shift exists under this store
        existing: Shift | None = await shift_repository.get_by_id(db, shift_id)
        if existing is None or existing.store_id != store_id:
            raise NotFoundError("Shift not found in this store")

        # 이름 변경 여부 확인 — Detect name change for cascade
        name_changed: bool = data.name is not None and data.name != existing.name

        # 이름 변경 시 중복 확인 — Check name uniqueness if changing name
        if name_changed:
            name_exists: bool = await shift_repository.exists(
                db, {"store_id": store_id, "name": data.name}
            )
            if name_exists:
                raise DuplicateError(
                    "A shift with this name already exists in this store"
                )

        update_data: dict = data.model_dump(exclude_unset=True)
        shift: Shift | None = await shift_repository.update(db, shift_id, update_data)
        if shift is None:
            raise NotFoundError("Shift not found")

        # 이름 변경 시 체크리스트 템플릿 제목 자동 업데이트
        # Cascade shift name change to checklist template titles
        if name_changed:
            await self._cascade_shift_name_to_templates(
                db, shift_id, store.name, data.name  # type: ignore[arg-type]
            )

        return self._to_response(shift)

    async def _cascade_shift_name_to_templates(
        self,
        db: AsyncSession,
        shift_id: UUID,
        store_name: str,
        new_shift_name: str,
    ) -> None:
        """시프트 이름 변경 시 관련 체크리스트 템플릿 제목을 자동 업데이트합니다.

        Update checklist template titles when a shift is renamed.
        Title format: '{store} - {shift} - {position}' or '{store} - {shift} - {position} (extra)'
        """
        result = await db.execute(
            sa_select(ChecklistTemplate).where(ChecklistTemplate.shift_id == shift_id)
        )
        templates = result.scalars().all()
        if not templates:
            return

        # 필요한 position 이름 일괄 조회 — Batch load position names
        position_ids = {t.position_id for t in templates}
        pos_result = await db.execute(
            sa_select(Position).where(Position.id.in_(position_ids))
        )
        positions_map: dict[UUID, str] = {
            p.id: p.name for p in pos_result.scalars().all()
        }

        for tmpl in templates:
            pos_name: str = positions_map.get(tmpl.position_id, "")
            new_base: str = f"{store_name} - {new_shift_name} - {pos_name}"
            # 기존 제목 끝의 괄호 부분 보존 — Preserve optional (extra) suffix
            match = re.search(r"\s*\(([^)]+)\)\s*$", tmpl.title)
            tmpl.title = f"{new_base} ({match.group(1)})" if match else new_base

    async def delete_shift(
        self,
        db: AsyncSession,
        shift_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """근무조를 삭제합니다.

        Delete a shift by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            shift_id: 근무조 ID (Shift UUID)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 근무조를 찾을 수 없을 때 (Shift not found)
        """
        await self._verify_store_ownership(db, store_id, organization_id)

        # 근무조가 이 매장에 속하는지 확인 — Verify shift belongs to this store
        existing: Shift | None = await shift_repository.get_by_id(db, shift_id)
        if existing is None or existing.store_id != store_id:
            raise NotFoundError("Shift not found in this store")

        deleted: bool = await shift_repository.delete(db, shift_id)
        if not deleted:
            raise NotFoundError("Shift not found")


# 싱글턴 인스턴스 — Singleton instance
shift_service: ShiftService = ShiftService()
