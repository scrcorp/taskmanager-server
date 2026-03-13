"""직원용 스케줄 기간 조회 라우터."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.models.user_store import UserStore
from app.schemas.schedule import SchedulePeriodResponse
from app.services.schedule_period_service import schedule_period_service

router: APIRouter = APIRouter()


@router.get("/schedule-periods", response_model=list[SchedulePeriodResponse])
async def list_my_schedule_periods(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    store_id: str | None = None,
    status: str | None = None,
) -> list[SchedulePeriodResponse]:
    """내 매장의 스케줄 기간 목록."""
    if store_id is not None:
        # 요청한 매장이 내 매장인지 확인
        check = await db.execute(
            select(UserStore.id).where(
                UserStore.user_id == current_user.id,
                UserStore.store_id == UUID(store_id),
            )
        )
        if check.scalar_one_or_none() is None:
            return []
        store_ids = [UUID(store_id)]
    else:
        # 내 모든 매장 조회
        result = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == current_user.id)
        )
        store_ids = list(result.scalars().all())
        if not store_ids:
            return []

    responses: list[SchedulePeriodResponse] = []
    for sid in store_ids:
        periods, _ = await schedule_period_service.list_periods(
            db,
            organization_id=current_user.organization_id,
            store_id=sid,
            status=status,
            page=1,
            per_page=100,
        )
        responses.extend(periods)

    # period_start 내림차순 정렬
    responses.sort(key=lambda p: p.period_start, reverse=True)
    return responses
