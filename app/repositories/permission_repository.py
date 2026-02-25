"""Permission 레포지토리 — 권한 조회 쿼리.

Permission Repository — queries for permission lookups.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import Permission, RolePermission


class PermissionRepository:
    """permissions / role_permissions 테이블 쿼리."""

    async def get_permissions_by_role_id(self, db: AsyncSession, role_id: UUID) -> set[str]:
        """role_id에 해당하는 permission code set 반환."""
        result = await db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        return {row[0] for row in result.all()}

    async def get_permission_by_code(self, db: AsyncSession, code: str) -> Permission | None:
        """code로 단일 permission 조회."""
        result = await db.execute(
            select(Permission).where(Permission.code == code)
        )
        return result.scalar_one_or_none()

    async def get_all_permissions(self, db: AsyncSession) -> list[Permission]:
        """전체 permission 목록 (resource, action 순 정렬)."""
        result = await db.execute(
            select(Permission).order_by(Permission.resource, Permission.action)
        )
        return list(result.scalars().all())

    async def get_role_permissions_with_details(self, db: AsyncSession, role_id: UUID) -> list[Permission]:
        """role의 permission 상세 목록."""
        result = await db.execute(
            select(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
            .order_by(Permission.resource, Permission.action)
        )
        return list(result.scalars().all())

    async def set_role_permissions(
        self, db: AsyncSession, role_id: UUID, permission_ids: list[UUID]
    ) -> None:
        """역할의 permission을 일괄 교체 (기존 삭제 → 새로 삽입)."""
        # 기존 삭제
        existing = await db.execute(
            select(RolePermission).where(RolePermission.role_id == role_id)
        )
        for rp in existing.scalars().all():
            await db.delete(rp)
        await db.flush()

        # 새로 삽입
        for perm_id in permission_ids:
            db.add(RolePermission(role_id=role_id, permission_id=perm_id))
        await db.flush()


# 싱글턴 인스턴스
permission_repository: PermissionRepository = PermissionRepository()
