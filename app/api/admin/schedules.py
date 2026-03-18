"""관리자 스케줄 라우터."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import (
    BulkAssignChecklistRequest, BulkAssignChecklistResult,
    FinalizeResult, ScheduleBulkCreate, ScheduleCreate,
    ScheduleResponse, ScheduleUpdate, ScheduleValidation,
)
from app.services.schedule_service import schedule_service

router: APIRouter = APIRouter()


@router.get("", response_model=dict)
async def list_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: str | None = None,
    user_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 100,
) -> dict:
    """스케줄 목록."""
    items, total = await schedule_service.list_entries(
        db, current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        user_id=UUID(user_id) if user_id else None,
        date_from=date_from, date_to=date_to,
        status=status, page=page, per_page=per_page,
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("", response_model=ScheduleResponse, status_code=201)
async def create_entry(
    data: ScheduleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> ScheduleResponse:
    """단일 스케줄 생성."""
    return await schedule_service.create_entry(
        db, current_user.organization_id, data, current_user.id,
    )


@router.post("/bulk", response_model=list[ScheduleResponse], status_code=201)
async def bulk_create(
    data: ScheduleBulkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> list[ScheduleResponse]:
    """벌크 스케줄 생성."""
    return await schedule_service.bulk_create(
        db, current_user.organization_id, data.entries, current_user.id,
    )


@router.post("/generate-from-requests", response_model=list[ScheduleResponse], status_code=201)
async def generate_from_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
    store_id: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ScheduleResponse]:
    """신청 기반 스케줄 자동생성."""
    from app.utils.exceptions import BadRequestError
    if not store_id or date_from is None or date_to is None:
        raise BadRequestError("store_id, date_from, date_to are required")
    return await schedule_service.generate_from_requests(
        db, current_user.organization_id, UUID(store_id), date_from, date_to, current_user.id,
    )


@router.get("/{entry_id}", response_model=ScheduleResponse)
async def get_entry(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> ScheduleResponse:
    """스케줄 상세."""
    return await schedule_service.get_entry(db, entry_id, current_user.organization_id)


@router.patch("/{entry_id}", response_model=ScheduleResponse)
async def update_entry(
    entry_id: UUID,
    data: ScheduleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """스케줄 수정."""
    return await schedule_service.update_entry(
        db, entry_id, current_user.organization_id, data,
    )


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:delete"))],
) -> None:
    """스케줄 삭제."""
    await schedule_service.delete_entry(db, entry_id, current_user.organization_id)


@router.post("/validate", response_model=ScheduleValidation)
async def validate_entry(
    data: ScheduleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> ScheduleValidation:
    """검증만 (저장 안함)."""
    return await schedule_service.validate_entry(db, current_user.organization_id, data)


@router.post("/assign-checklist", response_model=BulkAssignChecklistResult, status_code=200)
async def bulk_assign_checklist(
    data: BulkAssignChecklistRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> BulkAssignChecklistResult:
    """스케줄 일괄 체크리스트 할당/교체/제거.

    Bulk assign, replace, or remove checklist instances for the given schedules.
    - checklist_template_id provided: create or replace cl_instance for each schedule
    - checklist_template_id is null: remove existing cl_instances for each schedule
    """
    return await schedule_service.bulk_assign_checklist(
        db,
        organization_id=current_user.organization_id,
        schedule_ids=[UUID(sid) for sid in data.schedule_ids],
        checklist_template_id=UUID(data.checklist_template_id) if data.checklist_template_id else None,
    )
