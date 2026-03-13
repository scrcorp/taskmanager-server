"""직원 스케줄 조회 라우터."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import ScheduleResponse
from app.services.schedule_service import schedule_service

router: APIRouter = APIRouter()


@router.get("/schedules", response_model=list[ScheduleResponse])
async def list_my_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ScheduleResponse]:
    """내 확정 스케줄."""
    items, _ = await schedule_service.list_entries(
        db, current_user.organization_id,
        user_id=current_user.id,
        date_from=date_from, date_to=date_to,
        per_page=200,
    )
    return items
