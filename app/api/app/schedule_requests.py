"""직원 스케줄 신청 라우터."""

from datetime import date as date_type
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import (
    ScheduleRequestCopyLastPeriod, ScheduleRequestCreate,
    ScheduleRequestFromTemplate, ScheduleRequestResponse, ScheduleRequestUpdate,
)
from app.services.schedule_request_service import schedule_request_service

router: APIRouter = APIRouter()


@router.get("/schedule-requests", response_model=list[ScheduleRequestResponse])
async def list_my_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    period_id: str | None = None,
    date_from: date_type | None = None,
    date_to: date_type | None = None,
) -> list[ScheduleRequestResponse]:
    """내 스케줄 신청 목록."""
    return await schedule_request_service.list_requests_for_user(
        db, current_user.id,
        period_id=UUID(period_id) if period_id else None,
        date_from=date_from,
        date_to=date_to,
    )


@router.post("/schedule-requests", response_model=ScheduleRequestResponse, status_code=201)
async def create_request(
    data: ScheduleRequestCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRequestResponse:
    """스케줄 신청 제출."""
    result = await schedule_request_service.create_request(db, current_user.id, data)
    await db.commit()
    return result


@router.post("/schedule-requests/from-template", response_model=list[ScheduleRequestResponse], status_code=201)
async def create_from_template(
    data: ScheduleRequestFromTemplate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ScheduleRequestResponse]:
    """템플릿으로 자동 제출."""
    results = await schedule_request_service.create_requests_from_template(
        db, current_user.id, UUID(data.period_id), UUID(data.template_id),
    )
    await db.commit()
    return results


@router.post("/schedule-requests/copy-last-period", response_model=list[ScheduleRequestResponse], status_code=201)
async def copy_last_period(
    data: ScheduleRequestCopyLastPeriod,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ScheduleRequestResponse]:
    """지난 기간 복사."""
    results = await schedule_request_service.copy_last_period(
        db, current_user.id, UUID(data.period_id), UUID(data.store_id),
    )
    await db.commit()
    return results


@router.put("/schedule-requests/{request_id}", response_model=ScheduleRequestResponse)
async def update_request(
    request_id: UUID,
    data: ScheduleRequestUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRequestResponse:
    """스케줄 신청 수정 (submitted 상태만)."""
    result = await schedule_request_service.update_request(db, request_id, current_user.id, data)
    await db.commit()
    return result


@router.delete("/schedule-requests/{request_id}", status_code=204)
async def delete_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """신청 취소 (기간 open일때만)."""
    await schedule_request_service.delete_request(db, request_id, current_user.id)
    await db.commit()
