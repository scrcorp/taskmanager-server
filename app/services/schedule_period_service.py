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
            raise BadRequestError("A schedule period overlapping this date range already exists for this store")

        try:
            period = await schedule_period_repository.create(db, {
                "organization_id": organization_id,
                "store_id": store_id,
                "period_start": data.period_start,
                "period_end": data.period_end,
                "request_deadline": data.request_deadline,
                "status": "open",
                "created_by": created_by,
            })
            result = await self._to_response(db, period)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

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
            raise BadRequestError("Only open periods can be updated")

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
            raise BadRequestError("A schedule period overlapping this date range already exists for this store")

        try:
            updated = await schedule_period_repository.update(db, period_id, update_data, organization_id)
            if updated is None:
                raise NotFoundError("Schedule period not found")
            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def _transition_status(
        self, db: AsyncSession, period_id: UUID, organization_id: UUID, expected_from: str, to_status: str,
    ) -> SchedulePeriodResponse:
        period = await self._get_period_or_404(db, period_id, organization_id)
        if period.status != expected_from:
            raise BadRequestError(f"Cannot perform this action from status '{period.status}'. Expected status: '{expected_from}'.")
        try:
            period.status = to_status
            await db.flush()
            await db.refresh(period)
            result = await self._to_response(db, period)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def reopen(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        """마감 해제 — sv_draft/closed → open."""
        period = await self._get_period_or_404(db, period_id, organization_id)
        if period.status not in ("closed", "sv_draft"):
            raise BadRequestError(
                f"Cannot reopen from status '{period.status}'. Only 'closed' or 'sv_draft' can be reopened."
            )
        try:
            period.status = "open"
            await db.flush()
            await db.refresh(period)
            result = await self._to_response(db, period)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def close_requests(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        return await self._transition_status(db, period_id, organization_id, "open", "closed")

    async def start_draft(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        return await self._transition_status(db, period_id, organization_id, "closed", "sv_draft")

    async def submit_review(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        return await self._transition_status(db, period_id, organization_id, "sv_draft", "gm_review")

    async def get_by_store_and_date(
        self, db: AsyncSession, store_id: UUID, d: date,
    ) -> SchedulePeriod | None:
        """주어진 date를 포함하는 period를 store_id로 조회. 없으면 None."""
        result = await db.execute(
            select(SchedulePeriod)
            .where(
                SchedulePeriod.store_id == store_id,
                SchedulePeriod.period_start <= d,
                SchedulePeriod.period_end >= d,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def finalize(self, db: AsyncSession, period_id: UUID, organization_id: UUID) -> SchedulePeriodResponse:
        """확정 — gm_review → finalized, 스케줄 confirmed 처리."""
        period = await self._get_period_or_404(db, period_id, organization_id)
        if period.status != "gm_review":
            raise BadRequestError(f"Cannot perform this action from status '{period.status}'. Expected status: 'gm_review'.")

        try:
            # Finalize entries — mark all as confirmed (store + date range 기반)
            from app.services.schedule_service import schedule_service
            await schedule_service.finalize_period_entries(
                db, organization_id,
                store_id=period.store_id,
                date_from=period.period_start,
                date_to=period.period_end,
                approved_by=period.created_by or period_id,
            )

            period.status = "finalized"
            await db.flush()
            await db.refresh(period)
            result = await self._to_response(db, period)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


schedule_period_service: SchedulePeriodService = SchedulePeriodService()
