"""역할 레포지토리 — 역할 CRUD 및 중복 검사 쿼리.

Role Repository — CRUD and duplicate-check queries for roles.
Extends BaseRepository with Role-specific database operations.
"""

from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Role
from app.repositories.base import BaseRepository


class RoleRepository(BaseRepository[Role]):
    """역할 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the roles table.
    Provides organization-scoped role retrieval and duplicate checking.
    """

    def __init__(self) -> None:
        """RoleRepository를 초기화합니다.

        Initialize the RoleRepository with the Role model.
        """
        super().__init__(Role)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[Role]:
        """조직에 속한 모든 역할을 레벨 순으로 조회합니다.

        Retrieve all roles belonging to an organization, ordered by level.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[Role]: 역할 목록 (List of roles ordered by level)
        """
        query: Select = (
            select(Role)
            .where(Role.organization_id == organization_id)
            .order_by(Role.priority)
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def check_duplicate(
        self,
        db: AsyncSession,
        organization_id: UUID,
        name: str,
        priority: int,
        exclude_id: UUID | None = None,
    ) -> bool:
        """같은 조직 내 역할 이름 또는 priority 중복을 확인합니다."""
        query: Select = (
            select(func.count())
            .select_from(Role)
            .where(
                and_(
                    Role.organization_id == organization_id,
                    or_(Role.name == name, Role.priority == priority),
                )
            )
        )
        if exclude_id is not None:
            query = query.where(Role.id != exclude_id)

        count: int = (await db.execute(query)).scalar() or 0
        return count > 0


# 싱글턴 인스턴스 — Singleton instance
role_repository: RoleRepository = RoleRepository()
