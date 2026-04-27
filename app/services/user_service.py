"""사용자 서비스 — 사용자 CRUD 및 매장 배정 비즈니스 로직.

User Service — Business logic for user CRUD and store assignment operations.
Handles user management including creation, update, activation toggle,
and user-store association management.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.repositories.store_repository import store_repository
from app.repositories.role_repository import role_repository
from app.repositories.user_repository import user_repository
from app.schemas.organization import StoreResponse
from app.core.permissions import STAFF_PRIORITY, SV_PRIORITY
from app.schemas.user import (
    UserCreate,
    UserListResponse,
    UserResponse,
    UserStoreResponse,
    UserUpdate,
)
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError
from app.utils.password import hash_password


class UserService:
    """사용자 관련 비즈니스 로직을 처리하는 서비스.

    Service handling user business logic.
    Provides CRUD operations and store assignment management.
    """

    @staticmethod
    def _effective_rate(user_rate, org_rate) -> float | None:
        """effective hourly rate = user.hourly_rate (if set) → org default → None.

        DB 레벨에서는 상속 의미를 보존 (NULL은 '상속'). 응답 시점에만 계산.
        """
        if user_rate is not None:
            return float(user_rate)
        if org_rate is not None:
            return float(org_rate)
        return None

    def _to_response(self, user: User, org_rate: float | None = None) -> UserResponse:
        """사용자 모델을 상세 응답 스키마로 변환 (effective rate 포함)."""
        role: Role = user.role
        raw_rate = float(user.hourly_rate) if user.hourly_rate is not None else None
        return UserResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            email_verified=user.email_verified,
            role_name=role.name,
            role_priority=role.priority,
            hourly_rate=raw_rate,
            effective_hourly_rate=self._effective_rate(raw_rate, org_rate),
            is_active=user.is_active,
            created_at=user.created_at,
        )

    def _to_list_response(self, user: User, org_rate: float | None = None) -> UserListResponse:
        """사용자 모델을 목록 응답 스키마로 변환 (effective rate 포함)."""
        role: Role = user.role
        raw_rate = float(user.hourly_rate) if user.hourly_rate is not None else None
        return UserListResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            email_verified=user.email_verified,
            role_name=role.name,
            role_priority=role.priority,
            hourly_rate=raw_rate,
            effective_hourly_rate=self._effective_rate(raw_rate, org_rate),
            is_active=user.is_active,
            created_at=user.created_at,
        )

    async def _get_org_rate(self, db: AsyncSession, organization_id: UUID) -> float | None:
        """조직 default_hourly_rate 한 번만 조회 (effective 계산용)."""
        r = await db.execute(
            select(Organization.default_hourly_rate).where(Organization.id == organization_id)
        )
        val = r.scalar()
        return float(val) if val is not None else None

    async def list_users(
        self,
        db: AsyncSession,
        organization_id: UUID,
        filters: dict[str, UUID | bool | None] | None = None,
    ) -> list[UserListResponse]:
        """조직에 속한 사용자 목록을 필터 조건으로 조회합니다.

        List users in the organization with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            filters: 필터 딕셔너리 (store_id, role_id, is_active)
                     (Filter dict with optional store_id, role_id, is_active)

        Returns:
            list[UserListResponse]: 사용자 목록 (List of user list responses)
        """
        users: list[User] = await user_repository.get_by_org(
            db, organization_id, filters
        )
        org_rate = await self._get_org_rate(db, organization_id)
        return [self._to_list_response(u, org_rate) for u in users]

    async def get_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> UserResponse:
        """사용자 상세 정보를 조회합니다.

        Retrieve user detail with role information.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            UserResponse: 사용자 상세 응답 (User detail response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        user: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        org_rate = await self._get_org_rate(db, organization_id)
        return self._to_response(user, org_rate)

    async def create_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: UserCreate,
        caller: User | None = None,
    ) -> UserResponse:
        """새 사용자를 생성합니다.

        Create a new user within an organization.
        Caller can only create users with role priority strictly greater than their own.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            data: 사용자 생성 데이터 (User creation data)
            caller: 요청자 (Caller user for level-based access control)

        Returns:
            UserResponse: 생성된 사용자 응답 (Created user response)

        Raises:
            DuplicateError: 같은 사용자명이 이미 존재할 때
                            (When the username already exists)
            NotFoundError: 지정한 역할을 찾을 수 없을 때 (Role not found)
            ForbiddenError: 자기보다 높거나 같은 우선순위의 역할 지정 시도
                            (Attempting to assign a role at or above caller's priority)
        """
        # 사용자명 중복 확인 — Check username uniqueness within org
        exists: bool = await user_repository.exists(
            db, {"organization_id": organization_id, "username": data.username}
        )
        if exists:
            raise DuplicateError("Username already exists in this organization")

        # 역할 유효성 확인 — Validate role exists in org
        role: Role | None = await role_repository.get_by_id(
            db, UUID(data.role_id), organization_id
        )
        if role is None:
            raise NotFoundError("Role not found")

        # 하위 직급만 생성 가능
        if caller is not None and role.priority <= caller.role.priority:
            raise ForbiddenError("Cannot create a user with a role at or above your priority")

        password_hash: str = hash_password(data.password)

        # Auto-fill hourly_rate from org default if not provided
        hourly_rate = getattr(data, "hourly_rate", None)
        if hourly_rate is None:
            from app.models.organization import Organization as OrgModel
            org_row = await db.execute(
                select(OrgModel.default_hourly_rate).where(OrgModel.id == organization_id)
            )
            org_rate = org_row.scalar()
            hourly_rate = float(org_rate) if org_rate else None

        # Attendance device 용 clockin_pin 자동 발급 (organization 단위 unique)
        from app.services.attendance_device_service import generate_unique_clockin_pin

        clockin_pin = await generate_unique_clockin_pin(db, organization_id)

        try:
            create_data: dict = {
                "organization_id": organization_id,
                "role_id": UUID(data.role_id),
                "username": data.username,
                "full_name": data.full_name,
                "email": data.email,
                "password_hash": password_hash,
                "clockin_pin": clockin_pin,
            }
            if hourly_rate is not None:
                create_data["hourly_rate"] = hourly_rate

            user: User = await user_repository.create(
                db,
                create_data,
            )

            # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
            loaded: User | None = await user_repository.get_detail(
                db, user.id, organization_id
            )
            if loaded is None:
                raise NotFoundError("User not found after creation")

            org_rate = await self._get_org_rate(db, organization_id)
            result = self._to_response(loaded, org_rate)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        data: UserUpdate,
        caller: User | None = None,
    ) -> UserResponse:
        """사용자 정보를 수정합니다.

        Update an existing user's information.
        When changing role_id, caller can only assign roles below their own priority.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)
            caller: 요청자 (Caller user for level-based access control)

        Returns:
            UserResponse: 수정된 사용자 응답 (Updated user response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
            ForbiddenError: 자기보다 높거나 같은 우선순위의 역할 지정 시도
                            (Attempting to assign a role at or above caller's priority)
        """
        update_data: dict = data.model_dump(exclude_unset=True)

        # username 변경 시 조직 내 중복 검사 — Check username uniqueness within org
        if "username" in update_data and update_data["username"] is not None:
            new_username: str = update_data["username"].strip()
            if not new_username:
                raise BadRequestError("Username cannot be empty")
            update_data["username"] = new_username
            exists: bool = await user_repository.exists(
                db, {"organization_id": organization_id, "username": new_username}
            )
            if exists:
                # 자기 자신의 기존 username이면 무시
                current_user_obj: User | None = await user_repository.get_by_id(
                    db, user_id, organization_id
                )
                if current_user_obj is None or current_user_obj.username != new_username:
                    raise DuplicateError("Username already exists in this organization")

        # 이메일 변경 시 인증 상태 리셋 — Reset email_verified when email changes
        if "email" in update_data:
            current_user_obj_for_email: User | None = await user_repository.get_by_id(
                db, user_id, organization_id
            )
            if current_user_obj_for_email and update_data["email"] != current_user_obj_for_email.email:
                update_data["email_verified"] = False

        # role_id를 문자열에서 UUID로 변환 — Convert role_id from string to UUID
        if "role_id" in update_data and update_data["role_id"] is not None:
            role: Role | None = await role_repository.get_by_id(
                db, UUID(update_data["role_id"]), organization_id
            )
            if role is None:
                raise NotFoundError("Role not found")
            # 하위 직급만 지정 가능
            if caller is not None and role.priority <= caller.role.priority:
                raise ForbiddenError("Cannot assign a role at or above your priority")
            update_data["role_id"] = UUID(update_data["role_id"])

            # Staff로 변경 시 모든 매장의 is_manager 초기화
            if role.priority >= STAFF_PRIORITY:
                await user_repository.reset_manager_flags(db, user_id)

        try:
            user: User | None = await user_repository.update(
                db, user_id, update_data, organization_id
            )
            if user is None:
                raise NotFoundError("User not found")

            # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
            loaded: User | None = await user_repository.get_detail(
                db, user_id, organization_id
            )
            if loaded is None:
                raise NotFoundError("User not found")

            org_rate = await self._get_org_rate(db, organization_id)
            result = self._to_response(loaded, org_rate)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def toggle_active(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> UserResponse:
        """사용자 활성/비활성 상태를 토글합니다.

        Toggle a user's active/inactive status.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            UserResponse: 변경된 사용자 응답 (Updated user response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        user: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        try:
            # 현재 상태 반전 — Invert current status
            toggled: User | None = await user_repository.update(
                db, user_id, {"is_active": not user.is_active}, organization_id
            )
            if toggled is None:
                raise NotFoundError("User not found")

            # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
            loaded: User | None = await user_repository.get_detail(
                db, user_id, organization_id
            )
            if loaded is None:
                raise NotFoundError("User not found")

            org_rate = await self._get_org_rate(db, organization_id)
            result = self._to_response(loaded, org_rate)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> None:
        """사용자를 삭제합니다 (소프트 삭제: 비활성화).

        Delete a user (soft-delete: deactivate).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        user: User | None = await user_repository.get_by_id(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        try:
            # 소프트 삭제: 비활성화 — Soft-delete: deactivate user
            await user_repository.update(
                db, user_id, {"is_active": False}, organization_id
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def get_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> list[StoreResponse]:
        """사용자에게 배정된 매장 목록을 조회합니다.

        Retrieve all stores assigned to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[StoreResponse]: 배정된 매장 목록 (List of assigned store responses)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        # 사용자 존재 확인 — Verify user exists in org
        user: User | None = await user_repository.get_by_id(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        from app.models.user_store import UserStore

        assignments: list[UserStore] = await user_repository.get_user_store_assignments(db, user_id)
        # store 정보가 필요하므로 store 조회
        stores: list[Store] = await user_repository.get_user_stores(db, user_id)
        store_map = {s.id: s for s in stores}
        return [
            UserStoreResponse(
                id=str(a.store_id),
                organization_id=str(store_map[a.store_id].organization_id) if a.store_id in store_map else "",
                name=store_map[a.store_id].name if a.store_id in store_map else "",
                address=store_map[a.store_id].address if a.store_id in store_map else None,
                is_active=store_map[a.store_id].is_active if a.store_id in store_map else False,
                is_manager=a.is_manager,
                is_work_assignment=a.is_work_assignment,
                created_at=store_map[a.store_id].created_at if a.store_id in store_map else a.created_at,
            )
            for a in assignments
            if a.store_id in store_map
        ]

    async def sync_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        assignments: list[dict],
    ) -> None:
        """매장 배정 일괄 저장 (diff 기반).

        Args:
            assignments: [{"store_id": UUID, "is_manager": bool}, ...]

        Raises:
            NotFoundError: 사용자 또는 매장을 찾을 수 없을 때
            BadRequestError: Role별 규칙 위반 시
        """
        user_with_role: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user_with_role is None:
            raise NotFoundError("User not found")

        priority = user_with_role.role.priority

        # Role별 검증
        manager_count = sum(1 for a in assignments if a["is_manager"])

        if priority >= STAFF_PRIORITY and manager_count > 0:
            raise BadRequestError("Staff cannot be assigned as manager")

        if priority == SV_PRIORITY and manager_count > 1:
            raise BadRequestError("Supervisor can only manage one store")

        # 매장 존재 확인
        org_stores = await store_repository.get_by_org(db, organization_id)
        org_store_ids = {s.id for s in org_stores}
        for a in assignments:
            if a["store_id"] not in org_store_ids:
                raise NotFoundError(f"Store not found: {a['store_id']}")

        try:
            await user_repository.sync_user_stores(db, user_id, assignments)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def add_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        organization_id: UUID,
        caller: User | None = None,
    ) -> None:
        """사용자에게 매장을 배정합니다 (개별 API용, 하위호환).

        Staff는 근무매장만 가능 (is_manager=false).
        Supervisor는 관리매장 1개만.
        """
        user_with_role: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user_with_role is None:
            raise NotFoundError("User not found")

        store: Store | None = await store_repository.get_by_id(
            db, store_id, organization_id
        )
        if store is None:
            raise NotFoundError("Store not found")

        already_exists: bool = await user_repository.user_store_exists(
            db, user_id, store_id
        )
        if already_exists:
            raise DuplicateError("User is already assigned to this store")

        try:
            await user_repository.add_user_store(db, user_id, store_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def remove_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """사용자에게서 매장 배정을 해제합니다.

        Remove a store assignment from a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 배정 관계를 찾을 수 없을 때 (Assignment not found)
        """
        try:
            removed: bool = await user_repository.remove_user_store(db, user_id, store_id)
            if not removed:
                raise NotFoundError("User-store assignment not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
user_service: UserService = UserService()
