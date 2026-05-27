"""사용자 레포지토리 — 사용자 CRUD 및 매장 매핑 쿼리.

User Repository — CRUD and store mapping queries for users.
Extends BaseRepository with User-specific database operations
including filtering, eager loading, and user-store associations.
"""

from uuid import UUID

from sqlalchemy import Select, and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import OWNER_PRIORITY
from app.models.organization import Store
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """사용자 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the users table.
    Provides organization-scoped user retrieval, filtering, and store management.
    """

    def __init__(self) -> None:
        """UserRepository를 초기화합니다.

        Initialize the UserRepository with the User model.
        """
        super().__init__(User)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        filters: dict[str, UUID | bool | None] | None = None,
    ) -> list[User]:
        """조직에 속한 사용자 목록을 필터 조건으로 조회합니다.

        Retrieve users belonging to an organization with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            filters: 필터 딕셔너리 (store_id, role_id, is_active)
                     (Filter dict with optional store_id, role_id, is_active)

        Returns:
            list[User]: 사용자 목록 (List of users)
        """
        query: Select = (
            select(User)
            .options(selectinload(User.role))
            .where(User.organization_id == organization_id)
        )

        if filters:
            store_ids: list[UUID] | None = filters.get("store_ids")  # type: ignore[assignment]
            role_id: UUID | None = filters.get("role_id")  # type: ignore[assignment]
            is_active: bool | None = filters.get("is_active")  # type: ignore[assignment]

            if store_ids:
                # 해당 매장(들)의 스케줄 대상 직원 조회:
                #   - Work 체크된 user_stores 레코드 OR
                #   - Owner (role.priority == OWNER_PRIORITY) — 전 매장 접근권
                from app.core.permissions import OWNER_PRIORITY
                from app.models.user import Role

                query = (
                    query.outerjoin(UserStore, UserStore.user_id == User.id)
                    .join(Role, Role.id == User.role_id)
                    .where(
                        or_(
                            and_(
                                UserStore.store_id.in_(store_ids),
                                UserStore.is_work_assignment.is_(True),
                            ),
                            Role.priority == OWNER_PRIORITY,
                        )
                    )
                    .distinct()
                )
            if role_id is not None:
                query = query.where(User.role_id == role_id)
            if is_active is not None:
                query = query.where(User.is_active == is_active)

        query = query.order_by(User.created_at)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_detail(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> User | None:
        """사용자 상세 정보를 역할과 함께 조회합니다.

        Retrieve user detail with role eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 범위 필터 (Organization scope filter)

        Returns:
            User | None: 역할이 로드된 사용자 또는 None
                         (User with role loaded, or None)
        """
        query: Select = (
            select(User)
            .options(selectinload(User.role))
            .where(User.id == user_id, User.organization_id == organization_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[Store]:
        """사용자에게 배정된 매장 목록을 조회합니다.

        Retrieve all stores assigned to a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)

        Returns:
            list[Store]: 배정된 매장 목록 (List of assigned stores)
        """
        query: Select = (
            select(Store)
            .join(UserStore, UserStore.store_id == Store.id)
            .where(UserStore.user_id == user_id)
            .order_by(Store.created_at)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def add_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
    ) -> UserStore:
        """사용자에게 매장을 배정합니다.

        Assign a store to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)

        Returns:
            UserStore: 생성된 매핑 레코드 (Created association record)
        """
        user_store: UserStore = UserStore(user_id=user_id, store_id=store_id)
        db.add(user_store)
        await db.flush()
        await db.refresh(user_store)
        return user_store

    async def remove_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
    ) -> bool:
        """사용자에게서 매장 배정을 해제합니다.

        Remove a store assignment from a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the removal was successful)
        """
        query: Select = select(UserStore).where(
            and_(UserStore.user_id == user_id, UserStore.store_id == store_id)
        )
        result = await db.execute(query)
        user_store: UserStore | None = result.scalar_one_or_none()

        if user_store is None:
            return False

        await db.delete(user_store)
        await db.flush()
        return True

    async def get_user_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """사용자에게 배정된 매장 ID 목록을 반환합니다.

        Return the list of store IDs assigned to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)

        Returns:
            list[UUID]: 배정된 매장 ID 목록 (List of assigned store IDs)
        """
        result = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def get_managed_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """관리매장 ID 목록 (is_manager=true)."""
        result = await db.execute(
            select(UserStore.store_id).where(
                UserStore.user_id == user_id, UserStore.is_manager.is_(True)
            )
        )
        return list(result.scalars().all())

    async def get_work_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """근무매장 ID 목록 (모든 user_stores)."""
        result = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def get_user_store_assignments(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UserStore]:
        """사용자의 전체 매장 배정 레코드 조회."""
        result = await db.execute(
            select(UserStore).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def sync_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
        assignments: list[dict],
    ) -> None:
        """일괄 저장: 현재 상태와 diff 계산 후 추가/수정/삭제.

        Args:
            assignments: [
                {
                    "store_id": UUID,
                    "is_manager": bool,
                    "is_work_assignment": bool,
                },
                ...,
            ]
        """
        # 현재 상태 조회
        current = await self.get_user_store_assignments(db, user_id)
        current_map: dict[UUID, UserStore] = {us.store_id: us for us in current}

        # 목표 상태 — store_id로 인덱싱한 원본 dict 보존
        target_map: dict[UUID, dict] = {a["store_id"]: a for a in assignments}

        # 삭제: 현재에 있지만 목표에 없는 것
        to_delete = set(current_map.keys()) - set(target_map.keys())
        if to_delete:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == user_id,
                    UserStore.store_id.in_(to_delete),
                )
            )

        # 추가/수정
        for store_id, a in target_map.items():
            is_manager: bool = a["is_manager"]
            is_work: bool = a.get("is_work_assignment", True)

            existing = current_map.get(store_id)
            if existing is None:
                db.add(UserStore(
                    user_id=user_id,
                    store_id=store_id,
                    is_manager=is_manager,
                    is_work_assignment=is_work,
                ))
            else:
                if existing.is_manager != is_manager:
                    existing.is_manager = is_manager
                if existing.is_work_assignment != is_work:
                    existing.is_work_assignment = is_work

        await db.flush()

    async def reset_manager_flags(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> None:
        """사용자의 모든 매장에서 is_manager를 false로 초기화합니다."""
        await db.execute(
            update(UserStore)
            .where(UserStore.user_id == user_id, UserStore.is_manager.is_(True))
            .values(is_manager=False)
        )
        await db.flush()

    async def bulk_update_fields(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_ids: list[UUID],
        changes: dict,
    ) -> int:
        """여러 사용자의 컬럼을 일괄 변경 (조직 스코프).

        changes: {컬럼명: 값} — 호출 측에서 화이트리스트 검증 완료된 dict.
        org 에 속한 user_ids 만 변경. 반환: 실제 변경된 행 수.
        """
        if not user_ids or not changes:
            return 0
        result = await db.execute(
            update(User)
            .where(
                User.id.in_(user_ids),
                User.organization_id == organization_id,
            )
            .values(**changes)
        )
        await db.flush()
        return result.rowcount or 0

    async def bulk_assign_org_stores_to_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        *,
        is_manager: bool = True,
        is_work_assignment: bool = True,
    ) -> int:
        """조직 내 모든 매장을 user_stores 에 누락분만 INSERT. Owner / Super Owner 자동 배정용.

        룰: is_manager=true 이면 반드시 is_work_assignment=true (work 해제 불가).
        반환: 신규 INSERT 된 매장 수.
        """
        if is_manager:
            is_work_assignment = True
        store_rows = await db.execute(
            select(Store.id).where(Store.organization_id == organization_id)
        )
        store_ids = list(store_rows.scalars().all())
        if not store_ids:
            return 0

        existing_rows = await db.execute(
            select(UserStore.store_id).where(
                UserStore.user_id == user_id,
                UserStore.store_id.in_(store_ids),
            )
        )
        existing_ids = set(existing_rows.scalars().all())

        new_count = 0
        for sid in store_ids:
            if sid in existing_ids:
                continue
            db.add(UserStore(
                user_id=user_id,
                store_id=sid,
                is_manager=is_manager,
                is_work_assignment=is_work_assignment,
            ))
            new_count += 1
        await db.flush()
        return new_count

    async def bulk_assign_store_to_all_owners(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> int:
        """신규 매장을 조직의 모든 활성 Owner / Super Owner 에게 자동 배정.

        priority <= OWNER_PRIORITY 인 모든 사용자가 대상 (super_owner 포함).
        반환: 신규 INSERT 수.
        """
        owner_rows = await db.execute(
            select(User.id)
            .join(Role, User.role_id == Role.id)
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                Role.priority <= OWNER_PRIORITY,
            )
        )
        owner_ids = list(owner_rows.scalars().all())
        if not owner_ids:
            return 0

        existing_rows = await db.execute(
            select(UserStore.user_id).where(
                UserStore.store_id == store_id,
                UserStore.user_id.in_(owner_ids),
            )
        )
        existing_user_ids = set(existing_rows.scalars().all())

        new_count = 0
        for uid in owner_ids:
            if uid in existing_user_ids:
                continue
            db.add(UserStore(
                user_id=uid,
                store_id=store_id,
                is_manager=True,
                is_work_assignment=True,  # 룰: manager 면 work 도 자동 (해제 불가)
            ))
            new_count += 1
        await db.flush()
        return new_count

    async def remove_all_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        """사용자의 모든 user_stores 레코드 제거. Owner → 다른 role 강등 시 사용
        (Owner 자동 배정의 역동작 — 새 role 에 맞는 매장은 운영자가 다시 설정).
        반환: 삭제된 행 수.
        """
        result = await db.execute(
            delete(UserStore).where(UserStore.user_id == user_id)
        )
        await db.flush()
        return result.rowcount or 0

    async def user_store_exists(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
    ) -> bool:
        """사용자-매장 매핑이 존재하는지 확인합니다.

        Check if a user-store association already exists.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)

        Returns:
            bool: 매핑 존재 여부 (Whether the association exists)
        """
        query: Select = select(UserStore).where(
            and_(UserStore.user_id == user_id, UserStore.store_id == store_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none() is not None


# 싱글턴 인스턴스 — Singleton instance
user_repository: UserRepository = UserRepository()
