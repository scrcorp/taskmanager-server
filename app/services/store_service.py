"""매장 서비스 — 매장 CRUD 비즈니스 로직.

Store Service — Business logic for store CRUD operations.
Handles creation, retrieval, update, and deletion of stores
within an organization scope.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store
from app.repositories.store_repository import store_repository
from app.schemas.organization import (
    StoreCreate,
    StoreDetailResponse,
    StoreResponse,
    StoreUpdate,
    PositionResponse,
    ShiftResponse,
)
from app.utils.exceptions import DuplicateError, NotFoundError


class StoreService:
    """매장 관련 비즈니스 로직을 처리하는 서비스.

    Service handling store business logic.
    Provides CRUD operations scoped to the current organization.
    """

    def _to_response(self, store: Store) -> StoreResponse:
        """매장 모델을 응답 스키마로 변환합니다.

        Convert a Store model instance to a StoreResponse schema.

        Args:
            store: 매장 모델 (Store model instance)

        Returns:
            StoreResponse: 매장 응답 (Store response)
        """
        return StoreResponse(
            id=str(store.id),
            organization_id=str(store.organization_id),
            name=store.name,
            address=store.address,
            is_active=store.is_active,
            created_at=store.created_at,
        )

    async def list_stores(
        self,
        db: AsyncSession,
        organization_id: UUID,
        accessible_store_ids: list[UUID] | None = None,
    ) -> list[StoreResponse]:
        """조직에 속한 매장 목록을 조회합니다. 접근 가능한 매장만 필터링.

        List stores belonging to the organization, filtered by accessible stores.
        accessible_store_ids=None means full access (Owner).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            accessible_store_ids: 접근 가능한 매장 ID 목록, None=전체 (Accessible store IDs, None=all)

        Returns:
            list[StoreResponse]: 매장 목록 (List of store responses)
        """
        stores: list[Store] = await store_repository.get_by_org(db, organization_id)
        if accessible_store_ids is not None:
            stores = [s for s in stores if s.id in accessible_store_ids]
        return [self._to_response(s) for s in stores]

    async def get_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> StoreDetailResponse:
        """매장 상세 정보를 근무조/직책과 함께 조회합니다.

        Retrieve store detail with shifts and positions.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            StoreDetailResponse: 매장 상세 응답 (Store detail response)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
        """
        store: Store | None = await store_repository.get_detail(
            db, store_id, organization_id
        )
        if store is None:
            raise NotFoundError("Store not found")

        return StoreDetailResponse(
            id=str(store.id),
            organization_id=str(store.organization_id),
            name=store.name,
            address=store.address,
            is_active=store.is_active,
            created_at=store.created_at,
            shifts=[
                ShiftResponse(id=str(s.id), name=s.name, sort_order=s.sort_order)
                for s in store.shifts
            ],
            positions=[
                PositionResponse(id=str(p.id), name=p.name, sort_order=p.sort_order)
                for p in store.positions
            ],
        )

    async def create_store(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: StoreCreate,
    ) -> StoreResponse:
        """새 매장을 생성합니다.

        Create a new store within an organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 소속 조직 ID (Parent organization UUID)
            data: 매장 생성 데이터 (Store creation data)

        Returns:
            StoreResponse: 생성된 매장 응답 (Created store response)

        Raises:
            DuplicateError: 같은 이름의 매장이 이미 존재할 때
                            (When a store with the same name already exists)
        """
        # 같은 조직 내 매장명 중복 확인 — Check store name uniqueness within org
        exists: bool = await store_repository.exists(
            db, {"organization_id": organization_id, "name": data.name}
        )
        if exists:
            raise DuplicateError("A store with this name already exists")

        store: Store = await store_repository.create(
            db,
            {
                "organization_id": organization_id,
                "name": data.name,
                "address": data.address,
            },
        )
        return self._to_response(store)

    async def update_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: StoreUpdate,
    ) -> StoreResponse:
        """매장 정보를 수정합니다.

        Update an existing store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            StoreResponse: 수정된 매장 응답 (Updated store response)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
            DuplicateError: 같은 이름의 매장이 이미 존재할 때
                            (When a store with the same name already exists)
        """
        # 이름 변경 시 중복 확인 — Check name uniqueness if changing name
        if data.name is not None:
            existing: Store | None = await store_repository.get_by_id(
                db, store_id, organization_id
            )
            if existing is not None and existing.name != data.name:
                name_exists: bool = await store_repository.exists(
                    db, {"organization_id": organization_id, "name": data.name}
                )
                if name_exists:
                    raise DuplicateError("A store with this name already exists")

        update_data: dict = data.model_dump(exclude_unset=True)
        store: Store | None = await store_repository.update(
            db, store_id, update_data, organization_id
        )
        if store is None:
            raise NotFoundError("Store not found")

        return self._to_response(store)

    async def delete_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """매장을 삭제합니다.

        Delete a store by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
        """
        deleted: bool = await store_repository.delete(db, store_id, organization_id)
        if not deleted:
            raise NotFoundError("Store not found")


# 싱글턴 인스턴스 — Singleton instance
store_service: StoreService = StoreService()
