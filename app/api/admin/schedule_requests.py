"""관리자 스케줄 신청 라우터."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import (
    ScheduleConfirmRequest,
    ScheduleConfirmResult,
    ScheduleRequestAdminCreate,
    ScheduleRequestAdminUpdate,
    ScheduleRequestResponse,
    ScheduleRequestStatusUpdate,
)
from app.services.schedule_request_service import schedule_request_service

router: APIRouter = APIRouter()


@router.get("", response_model=dict)
async def list_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    period_id: str | None = None,
    store_id: str | None = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """직원 스케줄 신청 목록 조회."""
    items, total = await schedule_request_service.list_requests_admin(
        db,
        period_id=UUID(period_id) if period_id else None,
        store_id=UUID(store_id) if store_id else None,
        date_from=date_from,
        date_to=date_to,
        page=page, per_page=per_page,
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("", response_model=ScheduleRequestResponse)
async def admin_create_request(
    data: ScheduleRequestAdminCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> ScheduleRequestResponse:
    """관리자가 직접 스케줄 신청 생성 (staff에게 안 보임, confirm 시 entry로 변환)."""
    result = await schedule_request_service.admin_create_request(db, data, created_by=current_user.id)
    await db.commit()
    return result


@router.patch("/{request_id}", response_model=ScheduleRequestResponse)
async def admin_update_request(
    request_id: UUID,
    data: ScheduleRequestAdminUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleRequestResponse:
    """관리자가 스케줄 신청 수정 — 원본 추적 + auto-unmodify."""
    result = await schedule_request_service.admin_update_request(db, request_id, data)
    await db.commit()
    return result


@router.patch("/{request_id}/status", response_model=ScheduleRequestResponse)
async def update_request_status(
    request_id: UUID,
    data: ScheduleRequestStatusUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleRequestResponse:
    """직원 스케줄 신청 상태 변경."""
    result = await schedule_request_service.update_request_status(db, request_id, data.status)
    await db.commit()
    return result


@router.post("/{request_id}/revert", response_model=ScheduleRequestResponse)
async def admin_revert_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleRequestResponse:
    """Modified/rejected 신청을 원래 값으로 복원."""
    result = await schedule_request_service.admin_revert_request(db, request_id)
    await db.commit()
    return result


@router.delete("/{request_id}")
async def admin_delete_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:delete"))],
) -> dict:
    """관리자가 생성한 신청 삭제 (admin-created만)."""
    from app.repositories.schedule_request_repository import schedule_request_repository
    from app.utils.exceptions import BadRequestError, NotFoundError

    req = await schedule_request_repository.get_by_id(db, request_id)
    if req is None:
        raise NotFoundError("Request not found")
    if req.created_by is None:
        raise BadRequestError("직원이 제출한 신청은 삭제할 수 없습니다. Reject를 사용하세요.")
    await schedule_request_repository.delete(db, request_id)
    await db.commit()
    return {"ok": True}


@router.post("/confirm", response_model=ScheduleConfirmResult)
async def confirm_requests(
    data: ScheduleConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> ScheduleConfirmResult:
    """비거절 신청을 schedule로 일괄 변환 (GM confirm)."""
    result = await schedule_request_service.confirm_requests(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(data.store_id),
        date_from=data.date_from,
        date_to=data.date_to,
        confirmed_by=current_user.id,
        period_id=UUID(data.period_id) if data.period_id else None,
    )
    await db.commit()
    return result
