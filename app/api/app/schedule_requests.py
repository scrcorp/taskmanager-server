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
    ScheduleRequestBatchResult, ScheduleRequestBatchSubmit,
    ScheduleRequestCopyLastPeriod, ScheduleRequestCreate,
    ScheduleRequestFromTemplate, ScheduleRequestFromTemplateResult,
    ScheduleRequestResponse, ScheduleRequestUpdate,
)
from app.services.schedule_request_service import schedule_request_service

router: APIRouter = APIRouter()


@router.get("/schedule-requests", response_model=list[ScheduleRequestResponse])
async def list_my_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    date_from: date_type | None = None,
    date_to: date_type | None = None,
) -> list[ScheduleRequestResponse]:
    """내 스케줄 신청 목록."""
    return await schedule_request_service.list_requests_for_user(
        db, current_user.id,
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
    return await schedule_request_service.create_request(db, current_user.id, data)


@router.post("/schedule-requests/from-template", response_model=ScheduleRequestFromTemplateResult, status_code=201)
async def create_from_template(
    data: ScheduleRequestFromTemplate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRequestFromTemplateResult:
    """템플릿으로 자동 제출. on_conflict=skip(기본)/replace."""
    return await schedule_request_service.create_requests_from_template(
        db, current_user.id,
        store_id=UUID(data.store_id),
        date_from=data.date_from,
        date_to=data.date_to,
        template_id=UUID(data.template_id),
        on_conflict=data.on_conflict,
    )


@router.post("/schedule-requests/copy-last-period", response_model=ScheduleRequestFromTemplateResult, status_code=201)
async def copy_last_period(
    data: ScheduleRequestCopyLastPeriod,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRequestFromTemplateResult:
    """지난 주 신청 복사. on_conflict=skip(기본)/replace."""
    return await schedule_request_service.copy_last_period(
        db, current_user.id,
        store_id=UUID(data.store_id),
        date_from=data.date_from,
        date_to=data.date_to,
        on_conflict=data.on_conflict,
    )


@router.post("/schedule-requests/batch", response_model=ScheduleRequestBatchResult)
async def batch_submit_requests(
    data: ScheduleRequestBatchSubmit,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRequestBatchResult:
    """배치 제출 — 여러 신청을 한번에 생성/수정/삭제."""
    return await schedule_request_service.batch_submit(db, current_user.id, data)


@router.put("/schedule-requests/{request_id}", response_model=ScheduleRequestResponse)
async def update_request(
    request_id: UUID,
    data: ScheduleRequestUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScheduleRequestResponse:
    """스케줄 신청 수정 (submitted 상태만)."""
    return await schedule_request_service.update_request(db, request_id, current_user.id, data)


@router.delete("/schedule-requests/{request_id}", status_code=204)
async def delete_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """신청 취소 (기간 open일때만)."""
    await schedule_request_service.delete_request(db, request_id, current_user.id)
