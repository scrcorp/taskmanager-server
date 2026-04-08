"""조직 서비스 — 조직 조회 및 수정 비즈니스 로직.

Organization Service — Business logic for organization retrieval and update.
Provides current-organization scoped operations.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store
from app.models.user import User
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
            timezone=org.timezone,
            day_start_time=org.day_start_time.strftime("%H:%M") if org.day_start_time else None,
            weekly_overtime_limit=org.weekly_overtime_limit,
            default_hourly_rate=float(org.default_hourly_rate),
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

        default_hourly_rate 변경 시, 조직 내 모든 매장/사용자 중 rate가 null이거나
        새 org rate보다 낮은 대상을 자동으로 새 rate로 상향 보정 (cascade).
        """
        try:
            update_data: dict[str, str | bool | None] = data.model_dump(exclude_unset=True)

            # 현재 org의 rate 조회 (변경 전)
            prev_org = await organization_repository.get_by_id(db, organization_id)
            if prev_org is None:
                raise NotFoundError("Organization not found")
            new_rate_raw = update_data.get("default_hourly_rate")
            has_rate_change = new_rate_raw is not None and float(new_rate_raw) != float(prev_org.default_hourly_rate or 0)

            org: Organization | None = await organization_repository.update(
                db, organization_id, update_data
            )
            if org is None:
                raise NotFoundError("Organization not found")

            # Cascade: new org rate가 더 높으면 stores/users 자동 보정
            if has_rate_change:
                new_rate = float(org.default_hourly_rate)
                # 매장 cascade: store.default_hourly_rate IS NULL OR < new_rate → new_rate
                await db.execute(
                    update(Store)
                    .where(
                        Store.organization_id == organization_id,
                        (Store.default_hourly_rate.is_(None)) | (Store.default_hourly_rate < new_rate),
                    )
                    .values(default_hourly_rate=new_rate)
                )
                # 사용자 cascade: user.hourly_rate IS NULL OR < new_rate → new_rate
                await db.execute(
                    update(User)
                    .where(
                        User.organization_id == organization_id,
                        (User.hourly_rate.is_(None)) | (User.hourly_rate < new_rate),
                    )
                    .values(hourly_rate=new_rate)
                )

            result = OrganizationResponse(
                id=str(org.id),
                name=org.name,
                code=org.code,
                timezone=org.timezone,
                day_start_time=org.day_start_time.strftime("%H:%M") if org.day_start_time else None,
                weekly_overtime_limit=org.weekly_overtime_limit,
                default_hourly_rate=float(org.default_hourly_rate),
                is_active=org.is_active,
                created_at=org.created_at,
            )
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
organization_service: OrganizationService = OrganizationService()
