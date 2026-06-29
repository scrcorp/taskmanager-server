"""매장 서비스 — 매장 CRUD 비즈니스 로직.

Store Service — Business logic for store CRUD operations.
Handles creation, retrieval, update, and deletion of stores
within an organization scope.
"""

import re
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
from app.utils.exceptions import ConflictError, DuplicateError, NotFoundError


class StoreService:
    """매장 관련 비즈니스 로직을 처리하는 서비스.

    Service handling store business logic.
    Provides CRUD operations scoped to the current organization.
    """

    async def _generate_unique_code(
        self,
        db: AsyncSession,
        organization_id: UUID,
        name: str,
    ) -> str:
        """매장명에서 코드를 자동 생성합니다. 앞 3글자(영숫자), 충돌 시 2,3,4… 접미사.

        Derive a store code from its name: first 3 alphanumerics uppercased,
        appending 2/3/4… on org-scoped collision (e.g. SWC → SWC2 → SWC3).

        Args:
            db: 비동기 DB 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            name: 매장 이름 (Store name to derive from)

        Returns:
            str: org 내 유일한 코드 (2-10 alnum)
        """
        alnum: str = re.sub(r"[^A-Z0-9]", "", name.upper())
        base: str = alnum[:3]
        if len(base) < 2:
            # 한글 등 영숫자가 부족하면 'STO'로 폴백 (예: "세종점" → STO)
            base = (base + "STORE")[:3]

        candidate: str = base
        suffix: int = 2
        while await store_repository.code_exists(db, organization_id, candidate):
            candidate = f"{base}{suffix}"
            suffix += 1
        return candidate

    async def assert_open_for_create(self, db: AsyncSession, store_id: UUID) -> None:
        """closed(폐점) 매장엔 새 운영 데이터(스케줄/출근) 생성을 차단합니다.

        조회/수정/삭제는 허용 — 폐점은 "새로 만드는 것만" 막는다 (결정 2026-06-25).
        store 가 없으면 통과(상위에서 NotFound/FK 처리). closed 면 409.
        """
        from sqlalchemy import select
        deleted_at = await db.scalar(
            select(Store.deleted_at).where(Store.id == store_id)
        )
        if deleted_at is not None:
            raise ConflictError(
                "This store is closed and cannot accept new entries.",
                code="store_closed",
            )

    @staticmethod
    def _base_fields(store: Store) -> dict:
        """Store 모델 → 응답 공통 필드 dict.

        StoreResponse / StoreDetailResponse 가 공유하는 단일 매핑 출처.
        신규 컬럼은 여기 한 곳만 추가하면 list/detail 양쪽에 반영된다 (드리프트 방지).
        """
        return {
            "id": str(store.id),
            "organization_id": str(store.organization_id),
            "name": store.name,
            "code": store.code,
            "address": store.address,
            "phone": store.phone,
            "email": store.email,
            "status": store.status,
            "sort_order": store.sort_order,
            "is_active": store.is_active,
            "require_approval": store.require_approval,
            "operating_hours": store.operating_hours,
            "day_start_time": store.day_start_time,
            "max_work_hours_weekly": store.max_work_hours_weekly,
            "state_code": store.state_code,
            "timezone": store.timezone,
            "default_hourly_rate": float(store.default_hourly_rate) if store.default_hourly_rate is not None else None,
            "accepting_signups": store.accepting_signups,
            "created_at": store.created_at,
        }

    def _to_response(self, store: Store) -> StoreResponse:
        """매장 모델을 응답 스키마로 변환합니다 (Store → StoreResponse)."""
        return StoreResponse(**self._base_fields(store))

    async def list_stores(
        self,
        db: AsyncSession,
        organization_id: UUID,
        accessible_store_ids: list[UUID] | None = None,
        include_closed: bool = False,
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
        stores: list[Store] = await store_repository.get_by_org(
            db, organization_id, include_closed=include_closed
        )
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
            **self._base_fields(store),
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

        # 코드 결정 — 지정 시 org 내 중복 확인(폐점 코드 제외), 미지정 시 이름에서 자동 생성.
        if data.code is not None:
            if await store_repository.code_exists(db, organization_id, data.code):
                raise DuplicateError("A store with this code already exists")
            final_code: str = data.code
        else:
            final_code = await self._generate_unique_code(db, organization_id, data.name)

        # 신규 매장은 org 내 정렬 맨 뒤에 배치 (max sort_order + 1)
        next_sort_order: int = await store_repository.get_max_sort_order(db, organization_id) + 1

        create_data: dict = {
            "organization_id": organization_id,
            "name": data.name,
            "code": final_code,
            "address": data.address,
            "phone": data.phone,
            "email": data.email,
            "status": data.status,
            "sort_order": next_sort_order,
        }
        if data.timezone is not None:
            create_data["timezone"] = data.timezone
        if data.default_hourly_rate is not None:
            create_data["default_hourly_rate"] = data.default_hourly_rate
        try:
            store: Store = await store_repository.create(db, create_data)
            # 매장 생성 즉시 v0 (DEFAULT_FORM_CONFIG) published row 자동 삽입.
            # 매니저가 새 폼 만들고 publish 하면 v1, v2 ... 로 누적되며 그쪽이 current.
            from app.core.hiring import DEFAULT_FORM_CONFIG
            from app.models.hiring import StoreHiringForm

            v0 = StoreHiringForm(
                store_id=store.id,
                version=0,
                status="published",
                config=DEFAULT_FORM_CONFIG,
                is_current=True,
            )
            db.add(v0)

            # 신규 매장을 조직의 모든 활성 Owner / Super Owner 에게 자동 배정
            # (is_manager=true, is_work_assignment=true — manager 면 work 자동).
            from app.repositories.user_repository import user_repository
            await user_repository.bulk_assign_store_to_all_owners(
                db, store.id, organization_id
            )

            await db.commit()
            return self._to_response(store)
        except Exception:
            await db.rollback()
            raise

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
        # 이름/코드 변경 시 중복 확인 — Check name/code uniqueness if changing.
        fields = data.model_dump(exclude_unset=True)
        existing: Store | None = None
        if "name" in fields or "code" in fields:
            existing = await store_repository.get_by_id(db, store_id, organization_id)
        if data.name is not None:
            if existing is not None and existing.name != data.name:
                name_exists: bool = await store_repository.exists(
                    db, {"organization_id": organization_id, "name": data.name}
                )
                if name_exists:
                    raise DuplicateError("A store with this name already exists")
        if "code" in fields and data.code is not None:
            if existing is not None and existing.code != data.code:
                if await store_repository.code_exists(
                    db, organization_id, data.code, exclude_id=store_id
                ):
                    raise DuplicateError("A store with this code already exists")

        update_data: dict = data.model_dump(exclude_unset=True)
        # status=closed(폐점)는 soft-delete: deleted_at 기록. 다시 살아나면 해제.
        if "status" in update_data:
            from datetime import datetime, timezone as _tz
            from app.models.organization import STORE_STATUS_CLOSED
            if update_data["status"] == STORE_STATUS_CLOSED:
                update_data["deleted_at"] = datetime.now(_tz.utc)
            else:
                update_data["deleted_at"] = None
        try:
            store: Store | None = await store_repository.update(
                db, store_id, update_data, organization_id
            )
            if store is None:
                raise NotFoundError("Store not found")
            await db.commit()
            return self._to_response(store)
        except Exception:
            await db.rollback()
            raise

    async def reorder_stores(
        self,
        db: AsyncSession,
        organization_id: UUID,
        ordered_ids: list[UUID],
    ) -> int:
        """매장 표시 순서를 일괄 변경합니다. ordered_ids 순서대로 sort_order 부여.

        Reorder stores within an organization. org-scoped.

        Args:
            db: 비동기 DB 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            ordered_ids: 새 순서의 매장 ID 목록 (Store IDs in desired order)

        Returns:
            int: 갱신된 매장 수 (Number of stores updated)
        """
        try:
            updated: int = await store_repository.reorder(
                db, organization_id, ordered_ids
            )
            await db.commit()
            return updated
        except Exception:
            await db.rollback()
            raise

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
        try:
            deleted: bool = await store_repository.delete(db, store_id, organization_id)
            if not deleted:
                raise NotFoundError("Store not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
store_service: StoreService = StoreService()
