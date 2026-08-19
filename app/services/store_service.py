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
    NumberingRecalculateRequest,
    NumberingRecalculateResponse,
    NumberingUpdateRequest,
    NumberingUpdateResponse,
    StoreCreate,
    StoreDetailResponse,
    StoreResponse,
    StoreUpdate,
    PositionResponse,
    ShiftResponse,
)
from app.services import empid_cursor_service
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

    async def _validate_group_org(
        self, db: AsyncSession, group_id: UUID, organization_id: UUID
    ) -> None:
        """group_id 가 이 org 소속인지 검증 — 타 org 그룹 배정 차단 (404, 존재 누설 방지)."""
        from sqlalchemy import select
        from app.models.organization import StoreGroup

        org = await db.scalar(
            select(StoreGroup.organization_id).where(StoreGroup.id == group_id)
        )
        if org is None or org != organization_id:
            raise NotFoundError("Store group not found")

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
            # 영업시간은 더 이상 매장 컬럼이 아니다 — settings registry 키 `store.operating_hours`
            # 로 옮겼다 (D2-3). 컬럼은 미설정이 NULL 이라 호출부마다 폴백을 각자 짜게 되고,
            # 실제로 그래서 전 매장 NULL 로 방치됐다. registry 는 매장 → 조직 → 기본값 cascade 를 준다.
            "day_start_time": store.day_start_time,
            "max_work_hours_weekly": store.max_work_hours_weekly,
            "state_code": store.state_code,
            "timezone": store.timezone,
            "default_hourly_rate": float(store.default_hourly_rate) if store.default_hourly_rate is not None else None,
            "accepting_signups": store.accepting_signups,
            "group_id": str(store.group_id) if store.group_id else None,
            "number_range_start": store.number_range_start,
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
        responses = [self._to_response(s) for s in stores]
        # 채번 커서 현황 (§3-1) — org 당 몇 번의 집계 쿼리로 한 번에 붙인다(매장 수 무관).
        numbering = await empid_cursor_service.numbering_for_stores(
            db, [s.id for s in stores]
        )
        for store, response in zip(stores, responses):
            response.numbering = numbering.get(store.id)
        return responses

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

        detail = StoreDetailResponse(
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
        # 채번 커서 현황 (§3-1). Shared 그룹 소속이면 scope="group" + 그룹 id 가 온다 —
        # 콘솔은 이걸 보고 번호대 칸을 비활성화하고 수정 대상을 그룹으로 돌린다.
        detail.numbering = await empid_cursor_service.numbering_for_store(
            db, store_id
        )
        return detail

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
        # 같은 조직 내 매장명 중복 확인 — Check store name uniqueness within org.
        # 폐점 매장은 이름을 놓아준다(code 와 같은 기준) — 삭제가 소프트로 바뀐 뒤
        # 이 필터가 없으면 폐점한 매장의 이름을 다시 쓸 수 없다.
        exists: bool = await store_repository.name_exists(
            db, organization_id, data.name
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
        if data.group_id is not None:
            await self._validate_group_org(db, data.group_id, organization_id)
            create_data["group_id"] = data.group_id
        if data.number_range_start is not None:
            # Shared 그룹은 그룹 번호대 하나만 쓴다 — 매장값을 받아도 채번에서 무시되므로
            # 저장하지 않고 거절한다 (§4 ERR-RANGE-IGNORED, 조용한 실패 제거).
            await empid_cursor_service.assert_range_start_allowed(
                db, organization_id, data.group_id
            )
            create_data["number_range_start"] = data.number_range_start
        # 채번 커서 초기화 — 백필(마이그레이션)이 기존 행을 전부 채웠으므로 신규 행도
        # 여기서 채운다. 비워두면 코드에 NULL 폴백(= MAX 경로)이 되살아난다(O1).
        create_data["next_empid"] = empid_cursor_service.initial_cursor(
            data.number_range_start,
            await empid_cursor_service.group_range_start(db, data.group_id),
        )

        # 영업일 경계는 조직 기본값의 **스냅샷**이다 (D2-2). 라이브 cascade 가 아니다 —
        # cascade 로 두면 조직 기본값을 바꾸는 순간 기존 매장의 경계가 소리 없이 따라 움직이고,
        # 그것은 이미 확정된 과거 집계(급여 기간·일일 리포트)를 흔든다.
        # 조직이 미설정이면 매장도 미설정으로 두고 런타임 기본값(06:00)에 맡긴다.
        from sqlalchemy import select as _select
        from app.models.organization import Organization
        from app.utils.timezone import store_day_start_from_org

        org_day_start = await db.scalar(
            _select(Organization.day_start_time).where(Organization.id == organization_id)
        )
        day_start_snapshot = store_day_start_from_org(org_day_start)
        if day_start_snapshot is not None:
            create_data["day_start_time"] = day_start_snapshot

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

            # RULE-B — 그룹에 들어가면서 생기는 매장도 편입이다. 커서끼리 승격시킨다
            # (MAX(empid)+1 이 아니다 — 예외 번호가 그룹 커서를 밀어올린다).
            if store.group_id is not None:
                await empid_cursor_service.promote_group_cursor_on_join(
                    db, store.id, store.group_id
                )

            await db.commit()
            response = self._to_response(store)
            response.numbering = await empid_cursor_service.numbering_for_store(
                db, store.id
            )
            return response
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
                name_exists: bool = await store_repository.name_exists(
                    db, organization_id, data.name, exclude_id=store_id
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
        # group_id: 명시적 null=그룹 해제 허용, 값이 있으면 org 소속 검증 + str→UUID.
        group_changed: bool = False
        if "group_id" in update_data:
            # model_dump(python 모드)가 UUID 객체를 그대로 주므로 수동 변환 불필요 (잘못된 값은 422).
            if update_data["group_id"] is not None:
                await self._validate_group_org(db, update_data["group_id"], organization_id)
            from sqlalchemy import select as _select
            old_group = await db.scalar(
                _select(Store.group_id).where(Store.id == store_id)
            )
            group_changed = old_group != update_data["group_id"]
        # number_range_start 를 **넣으려는** 경우만 문맥 검사한다 (§4 ERR-RANGE-IGNORED).
        # 명시적 null(해제)은 그대로 허용 — 지우는 것은 조용한 실패가 아니다.
        if update_data.get("number_range_start") is not None:
            from sqlalchemy import select as _select
            target_group = (
                update_data["group_id"]
                if "group_id" in update_data
                else await db.scalar(_select(Store.group_id).where(Store.id == store_id))
            )
            await empid_cursor_service.assert_range_start_allowed(
                db, organization_id, target_group
            )
        try:
            store: Store | None = await store_repository.update(
                db, store_id, update_data, organization_id
            )
            if store is None:
                raise NotFoundError("Store not found")
            # RULE-B — 편입 시 그룹 커서를 승격 (group.cursor = max(group, store)).
            # 커밋 전에 돌려 편성 변경과 같은 트랜잭션에 묶는다.
            if group_changed and store.group_id is not None:
                await empid_cursor_service.promote_group_cursor_on_join(
                    db, store.id, store.group_id
                )
            await db.commit()
            response = self._to_response(store)
            response.numbering = await empid_cursor_service.numbering_for_store(
                db, store.id
            )
            # 그룹 편성 직후 공유 스코프 내 기존 empid 중복 경고 (블록하지 않음 — 정책 A).
            if group_changed and store.group_id is not None:
                from app.services.org_numbering import (
                    duplicate_empids_in_scope,
                    empid_scope_store_ids,
                )
                scope = await empid_scope_store_ids(db, store.id)
                response.duplicate_empids = await duplicate_empids_in_scope(db, scope)
            return response
        except Exception:
            await db.rollback()
            raise

    async def _assert_store_owns_cursor(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """이 매장이 자기 커서를 갖는지 확인. Shared 그룹 소속이면 거절한다.

        커서는 채번 스코프에 하나뿐이다 — Shared 그룹 매장의 커서는 쉬고 있으므로
        여기서 고치면 아무 일도 일어나지 않는다(조용한 실패). 계약의 numbering.scope
        가 콘솔에 이미 "group" 이라고 말해주므로, 서버도 같은 말을 한다.
        """
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if store is None:
            raise NotFoundError("Store not found")
        info = await empid_cursor_service.numbering_for_store(db, store_id)
        if info is not None and info.scope == empid_cursor_service.SCOPE_GROUP:
            from app.core.error_codes.empid import ERR_RANGE_IGNORED
            raise ERR_RANGE_IGNORED()

    async def update_numbering(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: NumberingUpdateRequest,
        actor_id: UUID | None,
    ) -> NumberingUpdateResponse:
        """매장 커서 수동 조정 (§3-2). 사유 필수, 낮추는 것도 허용(lowered=true)."""
        await self._assert_store_owns_cursor(db, store_id, organization_id)
        info, previous, lowered = await empid_cursor_service.set_cursor(
            db,
            scope=empid_cursor_service.SCOPE_STORE,
            scope_id=store_id,
            next_empid=data.next_empid,
            reason=data.reason,
            actor_id=actor_id,
        )
        return NumberingUpdateResponse(
            **info.model_dump(), previous=previous, lowered=lowered
        )

    async def recalculate_numbering(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: NumberingRecalculateRequest,
        actor_id: UUID | None,
    ) -> NumberingRecalculateResponse:
        """매장 커서 재계산 (§3-3). apply=false 면 미리보기, true 면 사유 필수."""
        await self._assert_store_owns_cursor(db, store_id, organization_id)
        info, applied, previous = await empid_cursor_service.recalculate_cursor(
            db,
            scope=empid_cursor_service.SCOPE_STORE,
            scope_id=store_id,
            apply=data.apply,
            reason=data.reason,
            actor_id=actor_id,
        )
        return NumberingRecalculateResponse(
            **info.model_dump(), applied=applied, previous=previous
        )

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
        """매장을 **폐점(soft delete)** 처리합니다 — status=closed + deleted_at (§3-7).

        Close a store: the row stays, only its status changes. 경로·메서드·204 는
        그대로고 **동작만** 바뀌었다 (콘솔 문구는 `Close store`).

        하드 삭제를 하면 배정 행이 FK 로 함께 사라져 그 매장이 점유하던 empid 가
        풀린다 — 폐점 매장도 번호를 계속 점유해야 재합류·재발급 충돌이 없다(정책 A).
        진짜 삭제(purge)가 필요하면 백오피스 도구로 분리한다.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 매장을 찾을 수 없을 때 (Store not found)
        """
        try:
            closed: bool = await store_repository.soft_delete(
                db, store_id, organization_id
            )
            if not closed:
                raise NotFoundError("Store not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
store_service: StoreService = StoreService()
