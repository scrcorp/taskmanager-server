"""스케줄 기간 서비스."""

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store
from app.models.schedule import SchedulePeriod
from app.models.user import User
from app.repositories.schedule_period_repository import schedule_period_repository
from app.repositories.store_repository import store_repository
from app.schemas.schedule import SchedulePeriodCreate, SchedulePeriodResponse, SchedulePeriodUpdate
from app.utils.exceptions import BadRequestError, NotFoundError


# Valid status transitions
VALID_TRANSITIONS: dict[str, str] = {
    "open": "closed",
    "closed": "sv_draft",
    "sv_draft": "gm_review",
    "gm_review": "finalized",
}


class SchedulePeriodService:

    async def _to_response(self, db: AsyncSession, period: SchedulePeriod) -> SchedulePeriodResponse:
        # Store name
        store_result = await db.execute(select(Store.name).where(Store.id == period.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        # Created by name
        created_by_name: str | None = None
        if period.created_by is not None:
            name_result = await db.execute(select(User.full_name).where(User.id == period.created_by))
            created_by_name = name_result.scalar()

        return SchedulePeriodResponse(
            id=str(period.id),
            organization_id=str(period.organization_id),
            store_id=str(period.store_id),
            store_name=store_name,
            period_start=period.period_start,
            period_end=period.period_end,
            request_deadline=period.request_deadline,
            status=period.status,
            created_by=str(period.created_by) if period.created_by else None,
            created_by_name=created_by_name,
            created_at=period.created_at,
            updated_at=period.updated_at,
        )

    async def _get_period_or_404(
        self, db: AsyncSession, period_id: UUID, organization_id: UUID,
    ) -> SchedulePeriod:
        period = await schedule_period_repository.get_by_id(db, period_id, organization_id)
        if period is None:
            raise NotFoundError("Schedule period not found")
        return period

    async def list_periods(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[SchedulePeriodResponse], int]:
        periods, total = await schedule_period_repository.get_by_filters(
            db, organization_id, store_id, status, page, per_page
        )
        responses = [await self._to_response(db, p) for p in periods]
        return responses, total

    async def create_period(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: SchedulePeriodCreate,
        created_by: UUID,
    ) -> SchedulePeriodResponse:
        store_id = UUID(data.store_id)
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if store is None:
            raise NotFoundError("Store not found")

        if data.period_start >= data.period_end:
            raise BadRequestError("period_start must be before period_end")

        # Overlap check
        if await schedule_period_repository.check_overlap(db, store_id, data.period_start, data.period_end):
            raise BadRequestError("이 매장에 해당 기간과 겹치는 스케줄 기간이 이미 존재합니다")

        period = await schedule_period_repository.create(db, {
            "organization_id": organization_id,
            "store_id": store_id,
            "period_start": data.period_start,
            "period_end": data.period_end,
            "request_deadline": data.request_deadline,
            "status": "open",
            "created_by": created_by,
        })
        return await self._to_response(db, period)

    async def get_period(
        self, db: AsyncSession, period_id: UUID, organization_id: UUID,
    ) -> SchedulePeriodResponse:
        period = await self._get_period_or_404(db, period_id, organization_id)
        return await self._to_response(db, period)

    async def update_period(
        self, db: AsyncSession, period_id: UUID, organization_id: UUID, data: SchedulePeriodUpdate,
    ) -> SchedulePeriodResponse:
        period = await self._get_period_or_404(db, period_id, organization_id)

        if period.status not in ("open",):
            raise BadRequestError("open 상태의 기간만 수정할 수 있습니다")

        update_data: dict = {}
        if data.period_start is not None:
            update_data["period_start"] = data.period_start
        if data.period_end is not None:
            update_data["period_end"] = data.period_end
        if data.request_deadline is not None:
            update_data["request_deadline"] = data.request_deadline

        if not update_data:
            return await self._to_response(db, period)

        # Overlap check with new dates
        new_start = update_data.get("period_start", period.period_start)
        new_end = update_data.get("period_end", period.period_end)
        if new_start >= new_end:
            raise BadRequestError("period_start must be before period_end")
        if await schedule_period_repository.check_overlap(db, period.store_id, new_start, new_end, period.id):
            raise BadRequestError("이 매장에 해당 기간과 겹치는 스케줄 기간이 이미 존재합니다")

        updated = await schedule_period_repository.update(db, period_id, update_data, organization_id)
        if updated is None:
            raise NotFoundError("Schedule period not found")
        return await self._to_response(db, updated)

    async def _transition_status(
        self, db: AsyncSession, period_id: UUID, organization_id: UUID, expected_from: str, to_status: str,
    ) -> SchedulePeriodResponse:
        period = await self._get_period_or_404(db, period_id, organization_id)
        if period.status != expected_from:
            raise BadRequestError(f"현재 상태({period.status})에서는 이 작업을 수행할 수 없습니다. '{expected_from}' 상태여야 합니다.")
        period.status = to_status
        await db.flush()
        await db.refresh(period)
        return await self._to_response(db, period)

    async def reopen(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        """마감 해제 — sv_draft/closed → open."""
        period = await self._get_period_or_404(db, period_id, organization_id)
        if period.status not in ("closed", "sv_draft"):
            raise BadRequestError(
                f"현재 상태({period.status})에서는 다시 열 수 없습니다. 'closed' 또는 'sv_draft' 상태여야 합니다."
            )
        period.status = "open"
        await db.flush()
        await db.refresh(period)
        return await self._to_response(db, period)

    async def close_requests(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        return await self._transition_status(db, period_id, organization_id, "open", "closed")

    async def start_draft(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        return await self._transition_status(db, period_id, organization_id, "closed", "sv_draft")

    async def submit_review(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        return await self._transition_status(db, period_id, organization_id, "sv_draft", "gm_review")

    async def finalize(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        """확정 — gm_review → finalized, 스케줄 confirmed 처리."""
        period = await self._get_period_or_404(db, period_id, organization_id)
        if period.status != "gm_review":
            raise BadRequestError(f"현재 상태({period.status})에서는 이 작업을 수행할 수 없습니다. 'gm_review' 상태여야 합니다.")

        # Finalize entries — mark all as confirmed
        from app.services.schedule_service import schedule_service
        await schedule_service.finalize_period_entries(db, organization_id, period_id, period.created_by or period_id)

        period.status = "finalized"
        await db.flush()
        await db.refresh(period)
        return await self._to_response(db, period)


schedule_period_service: SchedulePeriodService = SchedulePeriodService()
