"""역할 서비스 — 역할 CRUD 비즈니스 로직.

Role Service — Business logic for role CRUD operations.
Handles creation, retrieval, update, and deletion of roles
with duplicate name/level validation.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Role
from app.repositories.role_repository import role_repository
from app.schemas.user import RoleCreate, RoleResponse, RoleUpdate
from app.utils.exceptions import DuplicateError, NotFoundError


class RoleService:
    """역할 관련 비즈니스 로직을 처리하는 서비스.

    Service handling role business logic.
    Provides CRUD operations with name/level uniqueness enforcement.
    """

    def _to_response(self, role: Role) -> RoleResponse:
        """역할 모델을 응답 스키마로 변환합니다.

        Convert a Role model instance to a RoleResponse schema.

        Args:
            role: 역할 모델 (Role model instance)

        Returns:
            RoleResponse: 역할 응답 (Role response)
        """
        return RoleResponse(
            id=str(role.id),
            name=role.name,
            level=role.level,
            created_at=role.created_at,
        )

    async def list_roles(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[RoleResponse]:
        """조직에 속한 역할 목록을 조회합니다.

        List all roles belonging to the organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[RoleResponse]: 역할 목록 (List of role responses)
        """
        roles: list[Role] = await role_repository.get_by_org(db, organization_id)
        return [self._to_response(r) for r in roles]

    async def create_role(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: RoleCreate,
    ) -> RoleResponse:
        """새 역할을 생성합니다.

        Create a new role within an organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            data: 역할 생성 데이터 (Role creation data)

        Returns:
            RoleResponse: 생성된 역할 응답 (Created role response)

        Raises:
            DuplicateError: 같은 이름 또는 레벨의 역할이 이미 존재할 때
                            (When a role with the same name or level already exists)
        """
        # 이름/레벨 중복 확인 — Check name/level uniqueness
        is_duplicate: bool = await role_repository.check_duplicate(
            db, organization_id, data.name, data.level
        )
        if is_duplicate:
            raise DuplicateError("A role with this name or level already exists")

        role: Role = await role_repository.create(
            db,
            {
                "organization_id": organization_id,
                "name": data.name,
                "level": data.level,
            },
        )
        return self._to_response(role)

    async def update_role(
        self,
        db: AsyncSession,
        role_id: UUID,
        organization_id: UUID,
        data: RoleUpdate,
    ) -> RoleResponse:
        """역할 정보를 수정합니다.

        Update an existing role.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            role_id: 역할 ID (Role UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)

        Returns:
            RoleResponse: 수정된 역할 응답 (Updated role response)

        Raises:
            NotFoundError: 역할을 찾을 수 없을 때 (Role not found)
            DuplicateError: 같은 이름 또는 레벨의 역할이 이미 존재할 때
                            (When a role with the same name or level already exists)
        """
        # 기존 역할 확인 — Verify existing role
        existing: Role | None = await role_repository.get_by_id(
            db, role_id, organization_id
        )
        if existing is None:
            raise NotFoundError("Role not found")

        # 변경할 값으로 중복 확인 — Check duplicates with updated values
        check_name: str = data.name if data.name is not None else existing.name
        check_level: int = data.level if data.level is not None else existing.level

        is_duplicate: bool = await role_repository.check_duplicate(
            db, organization_id, check_name, check_level, exclude_id=role_id
        )
        if is_duplicate:
            raise DuplicateError("A role with this name or level already exists")

        update_data: dict = data.model_dump(exclude_unset=True)
        role: Role | None = await role_repository.update(
            db, role_id, update_data, organization_id
        )
        if role is None:
            raise NotFoundError("Role not found")

        return self._to_response(role)

    async def delete_role(
        self,
        db: AsyncSession,
        role_id: UUID,
        organization_id: UUID,
    ) -> None:
        """역할을 삭제합니다.

        Delete a role by its ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            role_id: 역할 ID (Role UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 역할을 찾을 수 없을 때 (Role not found)
        """
        deleted: bool = await role_repository.delete(db, role_id, organization_id)
        if not deleted:
            raise NotFoundError("Role not found")


# 싱글턴 인스턴스 — Singleton instance
role_service: RoleService = RoleService()
