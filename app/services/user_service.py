"""사용자 서비스 — 사용자 CRUD 및 브랜드 배정 비즈니스 로직.

User Service — Business logic for user CRUD and brand assignment operations.
Handles user management including creation, update, activation toggle,
and user-brand association management.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Brand
from app.models.user import Role, User
from app.repositories.brand_repository import brand_repository
from app.repositories.role_repository import role_repository
from app.repositories.user_repository import user_repository
from app.schemas.organization import BrandResponse
from app.schemas.user import (
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)
from app.utils.exceptions import DuplicateError, NotFoundError
from app.utils.password import hash_password


class UserService:
    """사용자 관련 비즈니스 로직을 처리하는 서비스.

    Service handling user business logic.
    Provides CRUD operations and brand assignment management.
    """

    def _to_response(self, user: User) -> UserResponse:
        """사용자 모델을 상세 응답 스키마로 변환합니다.

        Convert a User model instance to a UserResponse schema.
        Requires role relationship to be loaded.

        Args:
            user: 역할이 로드된 사용자 모델 (User model with role loaded)

        Returns:
            UserResponse: 사용자 상세 응답 (User detail response)
        """
        role: Role = user.role
        return UserResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            role_name=role.name,
            role_level=role.level,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    def _to_list_response(self, user: User) -> UserListResponse:
        """사용자 모델을 목록 응답 스키마로 변환합니다.

        Convert a User model instance to a UserListResponse schema.

        Args:
            user: 역할이 로드된 사용자 모델 (User model with role loaded)

        Returns:
            UserListResponse: 사용자 목록 항목 응답 (User list item response)
        """
        role: Role = user.role
        return UserListResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            role_name=role.name,
            role_level=role.level,
            is_active=user.is_active,
        )

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
            filters: 필터 딕셔너리 (brand_id, role_id, is_active)
                     (Filter dict with optional brand_id, role_id, is_active)

        Returns:
            list[UserListResponse]: 사용자 목록 (List of user list responses)
        """
        users: list[User] = await user_repository.get_by_org(
            db, organization_id, filters
        )
        return [self._to_list_response(u) for u in users]

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

        return self._to_response(user)

    async def create_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: UserCreate,
    ) -> UserResponse:
        """새 사용자를 생성합니다.

        Create a new user within an organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            data: 사용자 생성 데이터 (User creation data)

        Returns:
            UserResponse: 생성된 사용자 응답 (Created user response)

        Raises:
            DuplicateError: 같은 사용자명이 이미 존재할 때
                            (When the username already exists)
            NotFoundError: 지정한 역할을 찾을 수 없을 때 (Role not found)
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

        password_hash: str = hash_password(data.password)
        user: User = await user_repository.create(
            db,
            {
                "organization_id": organization_id,
                "role_id": UUID(data.role_id),
                "username": data.username,
                "full_name": data.full_name,
                "email": data.email,
                "password_hash": password_hash,
            },
        )

        # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
        loaded: User | None = await user_repository.get_detail(
            db, user.id, organization_id
        )
        if loaded is None:
            raise NotFoundError("User not found after creation")

        return self._to_response(loaded)

    async def update_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        data: UserUpdate,
    ) -> UserResponse:
        """사용자 정보를 수정합니다.

        Update an existing user's information.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            UserResponse: 수정된 사용자 응답 (Updated user response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        update_data: dict = data.model_dump(exclude_unset=True)

        # role_id를 문자열에서 UUID로 변환 — Convert role_id from string to UUID
        if "role_id" in update_data and update_data["role_id"] is not None:
            role: Role | None = await role_repository.get_by_id(
                db, UUID(update_data["role_id"]), organization_id
            )
            if role is None:
                raise NotFoundError("Role not found")
            update_data["role_id"] = UUID(update_data["role_id"])

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

        return self._to_response(loaded)

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

        return self._to_response(loaded)

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

        # 소프트 삭제: 비활성화 — Soft-delete: deactivate user
        await user_repository.update(
            db, user_id, {"is_active": False}, organization_id
        )

    async def get_user_brands(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> list[BrandResponse]:
        """사용자에게 배정된 브랜드 목록을 조회합니다.

        Retrieve all brands assigned to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[BrandResponse]: 배정된 브랜드 목록 (List of assigned brand responses)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        # 사용자 존재 확인 — Verify user exists in org
        user: User | None = await user_repository.get_by_id(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        brands: list[Brand] = await user_repository.get_user_brands(db, user_id)
        return [
            BrandResponse(
                id=str(b.id),
                organization_id=str(b.organization_id),
                name=b.name,
                address=b.address,
                is_active=b.is_active,
                created_at=b.created_at,
            )
            for b in brands
        ]

    async def add_user_brand(
        self,
        db: AsyncSession,
        user_id: UUID,
        brand_id: UUID,
        organization_id: UUID,
    ) -> None:
        """사용자에게 브랜드를 배정합니다.

        Assign a brand to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 사용자 또는 브랜드를 찾을 수 없을 때
                           (User or brand not found)
            DuplicateError: 이미 배정되어 있을 때 (Already assigned)
        """
        # 사용자 존재 확인 — Verify user exists in org
        user: User | None = await user_repository.get_by_id(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        # 브랜드 존재 확인 — Verify brand exists in org
        brand: Brand | None = await brand_repository.get_by_id(
            db, brand_id, organization_id
        )
        if brand is None:
            raise NotFoundError("Brand not found")

        # 중복 배정 확인 — Check for duplicate assignment
        already_exists: bool = await user_repository.user_brand_exists(
            db, user_id, brand_id
        )
        if already_exists:
            raise DuplicateError("User is already assigned to this brand")

        await user_repository.add_user_brand(db, user_id, brand_id)

    async def remove_user_brand(
        self,
        db: AsyncSession,
        user_id: UUID,
        brand_id: UUID,
        organization_id: UUID,
    ) -> None:
        """사용자에게서 브랜드 배정을 해제합니다.

        Remove a brand assignment from a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            brand_id: 브랜드 ID (Brand UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 배정 관계를 찾을 수 없을 때 (Assignment not found)
        """
        removed: bool = await user_repository.remove_user_brand(db, user_id, brand_id)
        if not removed:
            raise NotFoundError("User-brand assignment not found")


# 싱글턴 인스턴스 — Singleton instance
user_service: UserService = UserService()
