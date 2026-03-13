"""관리자 스케줄 기간 라우터."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import SchedulePeriodCreate, SchedulePeriodResponse, SchedulePeriodUpdate
from app.services.schedule_period_service import schedule_period_service

router: APIRouter = APIRouter()


@router.get("", response_model=dict)
async def list_periods(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """스케줄 기간 목록을 조회합니다."""
    items, total = await schedule_period_service.list_periods(
        db, current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        status=status, page=page, per_page=per_page,
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("", response_model=SchedulePeriodResponse, status_code=201)
async def create_period(
    data: SchedulePeriodCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> SchedulePeriodResponse:
    """새 스케줄 기간을 생성합니다."""
    result = await schedule_period_service.create_period(
        db, current_user.organization_id, data, current_user.id,
    )
    await db.commit()
    return result


@router.get("/{period_id}", response_model=SchedulePeriodResponse)
async def get_period(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> SchedulePeriodResponse:
    """스케줄 기간 상세를 조회합니다."""
    return await schedule_period_service.get_period(db, period_id, current_user.organization_id)


@router.patch("/{period_id}", response_model=SchedulePeriodResponse)
async def update_period(
    period_id: UUID,
    data: SchedulePeriodUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> SchedulePeriodResponse:
    """스케줄 기간을 수정합니다 (open 상태만)."""
    result = await schedule_period_service.update_period(
        db, period_id, current_user.organization_id, data,
    )
    await db.commit()
    return result


@router.post("/{period_id}/reopen", response_model=SchedulePeriodResponse)
async def reopen_period(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> SchedulePeriodResponse:
    """마감 해제 (sv_draft/closed → open)."""
    result = await schedule_period_service.reopen(db, period_id, current_user.organization_id)
    await db.commit()
    return result


@router.post("/{period_id}/close-requests", response_model=SchedulePeriodResponse)
async def close_requests(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> SchedulePeriodResponse:
    """신청 마감 (open → closed)."""
    result = await schedule_period_service.close_requests(db, period_id, current_user.organization_id)
    await db.commit()
    return result


@router.post("/{period_id}/start-draft", response_model=SchedulePeriodResponse)
async def start_draft(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> SchedulePeriodResponse:
    """SV 편집 시작 (closed → sv_draft)."""
    result = await schedule_period_service.start_draft(db, period_id, current_user.organization_id)
    await db.commit()
    return result


@router.post("/{period_id}/submit-review", response_model=SchedulePeriodResponse)
async def submit_review(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> SchedulePeriodResponse:
    """GM 리뷰 제출 (sv_draft → gm_review)."""
    result = await schedule_period_service.submit_review(db, period_id, current_user.organization_id)
    await db.commit()
    return result


@router.post("/{period_id}/finalize", response_model=SchedulePeriodResponse)
async def finalize_period(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> SchedulePeriodResponse:
    """확정 (gm_review → finalized)."""
    result = await schedule_period_service.finalize(db, period_id, current_user.organization_id)
    await db.commit()
    return result
