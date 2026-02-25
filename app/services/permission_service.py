"""Permission 서비스 — 권한 관리 비즈니스 로직.

Permission Service — Business logic for managing role permissions.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import Permission
from app.models.user import Role, User
from app.repositories.permission_repository import permission_repository
from app.repositories.role_repository import role_repository
from app.utils.exceptions import ForbiddenError, NotFoundError


class PermissionService:

    async def list_all_permissions(self, db: AsyncSession) -> list[dict]:
        """전체 permission 목록 조회."""
        perms = await permission_repository.get_all_permissions(db)
        return [
            {
                "id": str(p.id),
                "code": p.code,
                "resource": p.resource,
                "action": p.action,
                "description": p.description,
                "require_priority_check": p.require_priority_check,
            }
            for p in perms
        ]

    async def get_role_permissions(
        self, db: AsyncSession, role_id: UUID, organization_id: UUID
    ) -> list[dict]:
        """역할의 permission 목록 조회."""
        role = await role_repository.get_by_id(db, role_id, organization_id)
        if role is None:
            raise NotFoundError("Role not found")

        perms = await permission_repository.get_role_permissions_with_details(db, role_id)
        return [
            {
                "id": str(p.id),
                "code": p.code,
                "resource": p.resource,
                "action": p.action,
                "description": p.description,
                "require_priority_check": p.require_priority_check,
            }
            for p in perms
        ]

    async def update_role_permissions(
        self,
        db: AsyncSession,
        role_id: UUID,
        permission_codes: list[str],
        organization_id: UUID,
        caller: User,
    ) -> list[dict]:
        """역할의 permission을 일괄 업데이트."""
        target_role: Role | None = await role_repository.get_by_id(db, role_id, organization_id)
        if target_role is None:
            raise NotFoundError("Role not found")

        # caller의 priority < target role의 priority 여야 함
        if target_role.priority <= caller.role.priority:
            raise ForbiddenError("Cannot modify permissions of a role at or above your priority")

        # permission codes → ids 변환
        all_perms = await permission_repository.get_all_permissions(db)
        perm_map = {p.code: p.id for p in all_perms}

        permission_ids = []
        for code in permission_codes:
            if code not in perm_map:
                raise NotFoundError(f"Permission not found: {code}")
            permission_ids.append(perm_map[code])

        await permission_repository.set_role_permissions(db, role_id, permission_ids)

        # 업데이트된 결과 반환
        return await self.get_role_permissions(db, role_id, organization_id)


permission_service: PermissionService = PermissionService()
