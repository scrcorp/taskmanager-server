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
from app.utils.exceptions import DuplicateError, ForbiddenError, NotFoundError


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
            priority=role.priority,
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
        caller_priority: int = 1,
    ) -> RoleResponse:
        """새 역할을 생성합니다. caller보다 높은 priority(숫자가 큰) 역할만 생성 가능."""
        if data.priority <= caller_priority:
            raise ForbiddenError("Cannot create a role at or above your priority")

        is_duplicate: bool = await role_repository.check_duplicate(
            db, organization_id, data.name, data.priority
        )
        if is_duplicate:
            raise DuplicateError("A role with this name or priority already exists")

        role: Role = await role_repository.create(
            db,
            {
                "organization_id": organization_id,
                "name": data.name,
                "priority": data.priority,
            },
        )
        return self._to_response(role)

    async def update_role(
        self,
        db: AsyncSession,
        role_id: UUID,
        organization_id: UUID,
        data: RoleUpdate,
        caller_priority: int = 1,
    ) -> RoleResponse:
        """역할 정보를 수정합니다. caller보다 높은 priority(숫자가 큰) 역할만 수정 가능."""
        existing: Role | None = await role_repository.get_by_id(
            db, role_id, organization_id
        )
        if existing is None:
            raise NotFoundError("Role not found")

        if existing.priority <= caller_priority:
            raise ForbiddenError("Cannot modify a role at or above your priority")

        if data.priority is not None and data.priority <= caller_priority:
            raise ForbiddenError("Cannot set role priority at or above your priority")

        check_name: str = data.name if data.name is not None else existing.name
        check_priority: int = data.priority if data.priority is not None else existing.priority

        is_duplicate: bool = await role_repository.check_duplicate(
            db, organization_id, check_name, check_priority, exclude_id=role_id
        )
        if is_duplicate:
            raise DuplicateError("A role with this name or priority already exists")

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
        caller_priority: int = 1,
    ) -> None:
        """역할을 삭제합니다. caller보다 높은 priority(숫자가 큰) 역할만 삭제 가능."""
        existing: Role | None = await role_repository.get_by_id(
            db, role_id, organization_id
        )
        if existing is None:
            raise NotFoundError("Role not found")

        if existing.priority <= caller_priority:
            raise ForbiddenError("Cannot delete a role at or above your priority")

        deleted: bool = await role_repository.delete(db, role_id, organization_id)
        if not deleted:
            raise NotFoundError("Role not found")


# 싱글턴 인스턴스 — Singleton instance
role_service: RoleService = RoleService()
