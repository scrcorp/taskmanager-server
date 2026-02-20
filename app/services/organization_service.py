"""조직 서비스 — 조직 조회 및 수정 비즈니스 로직.

Organization Service — Business logic for organization retrieval and update.
Provides current-organization scoped operations.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.repositories.organization_repository import organization_repository
from app.schemas.organization import OrganizationResponse, OrganizationUpdate
from app.utils.exceptions import NotFoundError


class OrganizationService:
    """조직 관련 비즈니스 로직을 처리하는 서비스.

    Service handling organization business logic.
    Provides read and update operations scoped to the current organization.
    """

    async def get_current(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> OrganizationResponse:
        """현재 조직 정보를 조회합니다.

        Retrieve the current organization's details.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID from JWT)

        Returns:
            OrganizationResponse: 조직 응답 (Organization response)

        Raises:
            NotFoundError: 조직을 찾을 수 없을 때 (Organization not found)
        """
        org: Organization | None = await organization_repository.get_by_id(
            db, organization_id
        )
        if org is None:
            raise NotFoundError("Organization not found")

        return OrganizationResponse(
            id=str(org.id),
            name=org.name,
            code=org.code,
            is_active=org.is_active,
            created_at=org.created_at,
        )

    async def update_current(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: OrganizationUpdate,
    ) -> OrganizationResponse:
        """현재 조직 정보를 수정합니다.

        Update the current organization's details.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID from JWT)
            data: 수정할 데이터 (Update data)

        Returns:
            OrganizationResponse: 수정된 조직 응답 (Updated organization response)

        Raises:
            NotFoundError: 조직을 찾을 수 없을 때 (Organization not found)
        """
        update_data: dict[str, str | bool | None] = data.model_dump(exclude_unset=True)
        org: Organization | None = await organization_repository.update(
            db, organization_id, update_data
        )
        if org is None:
            raise NotFoundError("Organization not found")

        return OrganizationResponse(
            id=str(org.id),
            name=org.name,
            code=org.code,
            is_active=org.is_active,
            created_at=org.created_at,
        )


# 싱글턴 인스턴스 — Singleton instance
organization_service: OrganizationService = OrganizationService()
